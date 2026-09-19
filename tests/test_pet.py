"""宠物对话、专家路由与会话历史。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import MAX_CHAT_MESSAGE_LENGTH


def chat(client: TestClient, auth: dict[str, str], message: str, **extra) -> dict:
    payload = {"message": message, **extra}
    response = client.post("/v1/pet/chat", json=payload, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_image_expert_routes_and_returns_suggestions(client: TestClient, auth: dict[str, str]) -> None:
    data = chat(client, auth, "找几张猫耳的图")
    assert data["expert"] == "image"
    assert data["intent"] == "search_image"
    assert data["live2d_action"] == "think"
    assert data["suggestions"]
    assert all(item["type"] == "image" for item in data["suggestions"])


def test_search_with_no_hit_still_replies(client: TestClient, auth: dict[str, str]) -> None:
    data = chat(client, auth, "找几张 zzzz 的图")
    assert data["expert"] == "image"
    assert data["suggestions"] == []
    assert "zzzz" in data["reply"]


def test_bilibili_expert(client: TestClient, auth: dict[str, str]) -> None:
    data = chat(client, auth, "今天有什么视频")
    assert data["expert"] == "bilibili"
    assert all(item["type"] == "video" for item in data["suggestions"])


def test_game_expert(client: TestClient, auth: dict[str, str]) -> None:
    data = chat(client, auth, "今天有什么新游戏")
    assert data["expert"] == "game"
    assert data["intent"] == "game_today"
    assert all(item["type"] == "game" for item in data["suggestions"])


def test_fact_expert_returns_card(client: TestClient, auth: dict[str, str]) -> None:
    data = chat(client, auth, "讲个冷知识")
    assert data["expert"] == "fact"
    assert data["live2d_action"] == "remind"
    assert [item["type"] for item in data["suggestions"]] == ["card"]
    assert data["suggestions"][0]["payload"]["target_type"] == "card"


def test_greeting_and_help(client: TestClient, auth: dict[str, str]) -> None:
    assert chat(client, auth, "你好")["intent"] == "greeting"
    assert chat(client, auth, "你能做什么")["intent"] == "help"
    assert chat(client, auth, "谢谢")["live2d_action"] == "happy"


def test_unknown_intent_is_confused(client: TestClient, auth: dict[str, str]) -> None:
    data = chat(client, auth, "咔嚓咔嚓")
    assert data["intent"] == "unknown"
    assert data["live2d_action"] == "confused"
    assert data["suggestions"] == []


def test_blocked_topic_is_refused(client: TestClient, auth: dict[str, str]) -> None:
    data = chat(client, auth, "给我来点 r18 内容")
    assert data["intent"] == "blocked"
    assert data["expert"] == "general"
    assert data["live2d_action"] == "confused"
    assert data["suggestions"] == []
    assert "r18" not in data["reply"].lower()


def test_context_resolves_current_image(client: TestClient, auth: dict[str, str]) -> None:
    data = chat(client, auth, "这张图是谁画的", context={"content_type": "image", "content_id": "img_0001"})
    assert data["intent"] == "context_image"
    assert data["live2d_action"] == "tap"
    assert data["suggestions"][0]["id"] == "img_0001"


def test_empty_message_is_bad_request(client: TestClient, auth: dict[str, str]) -> None:
    assert client.post("/v1/pet/chat", json={"message": ""}, headers=auth).status_code == 422
    response = client.post("/v1/pet/chat", json={"message": "   "}, headers=auth)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_overlong_message_is_validation_failed(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post("/v1/pet/chat", json={"message": "啊" * (MAX_CHAT_MESSAGE_LENGTH + 1)}, headers=auth)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_conversation_lifecycle(client: TestClient, auth: dict[str, str]) -> None:
    first = chat(client, auth, "你好")
    conversation_id = first["conversation_id"]

    second = chat(client, auth, "找几张猫耳的图", conversation_id=conversation_id)
    assert second["conversation_id"] == conversation_id

    conversations = client.get("/v1/pet/conversations", headers=auth).json()["data"]["items"]
    assert len(conversations) == 1
    assert conversations[0]["message_count"] == 4

    messages = client.get(
        f"/v1/pet/conversations/{conversation_id}/messages", headers=auth
    ).json()["data"]["items"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1]["expert"] == "general"
    assert messages[0]["expert"] is None


def test_unknown_conversation_is_not_found(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/pet/conversations/conv_missing/messages", headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_conversations_are_isolated_per_user(
    client: TestClient, auth: dict[str, str], second_user: dict[str, str]
) -> None:
    conversation_id = chat(client, auth, "你好")["conversation_id"]
    assert client.get("/v1/pet/conversations", headers=second_user).json()["data"]["items"] == []
    assert client.get(
        f"/v1/pet/conversations/{conversation_id}/messages", headers=second_user
    ).status_code == 404


def test_conversations_pagination(client: TestClient, auth: dict[str, str]) -> None:
    for index in range(3):
        chat(client, auth, f"你好 {index}")
    page = client.get("/v1/pet/conversations?limit=2", headers=auth).json()
    assert len(page["data"]["items"]) == 2
    assert page["meta"]["has_more"] is True
    rest = client.get(
        f"/v1/pet/conversations?limit=2&cursor={page['meta']['next_cursor']}", headers=auth
    ).json()
    assert len(rest["data"]["items"]) == 1
    assert rest["meta"]["has_more"] is False


def test_chat_requires_auth(client: TestClient) -> None:
    assert client.post("/v1/pet/chat", json={"message": "你好"}).status_code == 401
