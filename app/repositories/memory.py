"""内存仓储：模拟期唯一实现。

进程内单例，`POST /v1/dev/reset` 会清空全部用户数据。
"""

from __future__ import annotations

import secrets
import threading
from datetime import timedelta
from typing import Any

from app.core.timeutil import iso, now


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def new_token() -> str:
    return "anon_" + secrets.token_urlsafe(24)


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    # --- 生命周期 ---

    def reset(self) -> dict[str, int]:
        with self._lock:
            cleared = {
                "users": len(getattr(self, "users", {})),
                "favorites": sum(len(v) for v in getattr(self, "favorites", {}).values()),
                "history": sum(len(v) for v in getattr(self, "history", {}).values()),
                "conversations": len(getattr(self, "conversations", {})),
            }
            self.users: dict[str, dict[str, Any]] = {}
            self.tokens: dict[str, str] = {}
            self.favorites: dict[str, set[str]] = {}
            self.favorite_counts: dict[str, int] = {}
            self.history: dict[str, list[dict[str, Any]]] = {}
            self.conversations: dict[str, dict[str, Any]] = {}
            self.messages: dict[str, list[dict[str, Any]]] = {}
            self.user_conversations: dict[str, list[str]] = {}
            return cleared

    # --- 用户 ---

    def create_user(
        self,
        *,
        install_id: str | None,
        platform: str | None,
        app_version: str | None,
        ttl_hours: int,
        preferences: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            user_id = new_id("user")
            token = new_token()
            record = {
                "user_id": user_id,
                "install_id": install_id,
                "platform": platform,
                "app_version": app_version,
                "created_at": now(),
                "preferences": preferences
                or {"tags": [], "platforms": [], "genres": [], "safe_mode": True},
                "access_token": token,
                "expires_at": now() + timedelta(hours=ttl_hours),
            }
            self.users[user_id] = record
            self.tokens[token] = user_id
            return record

    def find_by_install_id(self, install_id: str) -> dict[str, Any] | None:
        with self._lock:
            for record in self.users.values():
                if record["install_id"] == install_id:
                    return record
            return None

    def rotate_token(self, user_id: str, ttl_hours: int) -> dict[str, Any]:
        with self._lock:
            record = self.users[user_id]
            self.tokens.pop(record["access_token"], None)
            token = new_token()
            record["access_token"] = token
            record["expires_at"] = now() + timedelta(hours=ttl_hours)
            self.tokens[token] = user_id
            return record

    def resolve_token(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            user_id = self.tokens.get(token)
            if user_id is None:
                return None
            return self.users.get(user_id)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.users.get(user_id)

    def update_preferences(self, user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            record = self.users[user_id]
            prefs = dict(record["preferences"])
            for key, value in patch.items():
                if value is not None:
                    prefs[key] = value
            record["preferences"] = prefs
            return prefs

    # --- 历史 ---

    def append_history(self, user_id: str, entry: dict[str, Any]) -> None:
        with self._lock:
            bucket = self.history.setdefault(user_id, [])
            bucket.insert(0, entry)
            del bucket[200:]

    def list_history(self, user_id: str, kind: str | None) -> list[dict[str, Any]]:
        with self._lock:
            bucket = self.history.get(user_id, [])
            if kind is None:
                return list(bucket)
            return [item for item in bucket if item["kind"] == kind]

    def delete_history(self, user_id: str, kind: str | None) -> int:
        with self._lock:
            bucket = self.history.get(user_id, [])
            if kind is None:
                deleted = len(bucket)
                self.history[user_id] = []
                return deleted
            keep = [item for item in bucket if item["kind"] != kind]
            deleted = len(bucket) - len(keep)
            self.history[user_id] = keep
            return deleted

    # --- 收藏（幂等） ---

    def set_favorite(self, user_id: str, content_id: str, favorited: bool) -> bool:
        with self._lock:
            bucket = self.favorites.setdefault(user_id, set())
            count = self.favorite_counts.get(content_id, 0)
            if favorited:
                if content_id not in bucket:
                    bucket.add(content_id)
                    count += 1
            else:
                if content_id in bucket:
                    bucket.discard(content_id)
                    count = max(0, count - 1)
            self.favorite_counts[content_id] = count
            return content_id in bucket

    def is_favorited(self, user_id: str, content_id: str) -> bool:
        with self._lock:
            return content_id in self.favorites.get(user_id, set())

    def favorite_count(self, content_id: str) -> int:
        with self._lock:
            return self.favorite_counts.get(content_id, 0)

    def list_favorites(self, user_id: str) -> list[str]:
        with self._lock:
            return sorted(self.favorites.get(user_id, set()))

    # --- 会话 ---

    def create_conversation(self, user_id: str, title: str | None) -> dict[str, Any]:
        with self._lock:
            conversation_id = new_id("conv")
            moment = now()
            record = {
                "id": conversation_id,
                "title": title,
                "created_at": moment,
                "updated_at": moment,
                "message_count": 0,
            }
            self.conversations[conversation_id] = record
            self.user_conversations.setdefault(user_id, []).insert(0, conversation_id)
            self.messages[conversation_id] = []
            return record

    def get_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self._lock:
            if conversation_id not in self.user_conversations.get(user_id, []):
                return None
            return self.conversations.get(conversation_id)

    def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            ids = self.user_conversations.get(user_id, [])
            return [self.conversations[cid] for cid in ids if cid in self.conversations]

    def append_message(self, user_id: str, conversation_id: str, message: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            bucket = self.messages.setdefault(conversation_id, [])
            bucket.append(message)
            record = self.conversations.get(conversation_id)
            if record is not None:
                record["updated_at"] = message["created_at"]
                record["message_count"] = len(bucket)
            return message

    def list_messages(self, user_id: str, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.messages.get(conversation_id, []))


store = MemoryStore()


def format_timestamp(value: Any) -> str:
    return iso(value)
