"""用户互动：偏好、收藏与历史。"""

from __future__ import annotations

from typing import Any

from app.core.timeutil import now
from app.repositories.memory import new_id, store


def preferences_for(user_id: str) -> dict[str, Any]:
    record = store.get_user(user_id)
    if record is None:
        return {"tags": [], "platforms": [], "genres": [], "safe_mode": True}
    return dict(record["preferences"])


def safe_mode_for(user_id: str) -> bool:
    return bool(preferences_for(user_id).get("safe_mode", True))


def update_preferences(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    return store.update_preferences(user_id, patch)


def favorited_ids(user_id: str) -> frozenset[str]:
    return frozenset(store.list_favorites(user_id))


def set_favorite(user_id: str, content_id: str, favorited: bool) -> dict[str, Any]:
    state = store.set_favorite(user_id, content_id, favorited)
    return {
        "id": content_id,
        "is_favorited": state,
        "favorite_count": store.favorite_count(content_id),
    }


def record_browse(user_id: str, item: dict[str, Any]) -> None:
    store.append_history(
        user_id,
        {
            "id": new_id("hist"),
            "kind": "browse",
            "query": None,
            "content": item,
            "created_at": now(),
        },
    )


def record_search(user_id: str, query: str) -> None:
    store.append_history(
        user_id,
        {
            "id": new_id("hist"),
            "kind": "search",
            "query": query,
            "content": None,
            "created_at": now(),
        },
    )


def history_entries(user_id: str, kind: str | None) -> list[dict[str, Any]]:
    return store.list_history(user_id, kind)


def delete_history(user_id: str, kind: str | None) -> dict[str, Any]:
    return {"deleted": store.delete_history(user_id, kind), "kind": kind}
