"""仓储接口定义。

内存实现（memory.py）覆盖模拟期全部能力；MySQL 实现在 P1 阶段按同一接口落地，
业务层只依赖这里的 Protocol，不依赖具体存储。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UserRepository(Protocol):
    def create_user(self, *, install_id: str | None, platform: str | None, app_version: str | None, ttl_hours: int) -> dict[str, Any]: ...

    def find_by_install_id(self, install_id: str) -> dict[str, Any] | None: ...

    def rotate_token(self, user_id: str, ttl_hours: int) -> dict[str, Any]: ...

    def resolve_token(self, token: str) -> dict[str, Any] | None: ...

    def get_user(self, user_id: str) -> dict[str, Any] | None: ...

    def update_preferences(self, user_id: str, patch: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class InteractionRepository(Protocol):
    def append_history(self, user_id: str, entry: dict[str, Any]) -> None: ...

    def list_history(self, user_id: str, kind: str | None) -> list[dict[str, Any]]: ...

    def delete_history(self, user_id: str, kind: str | None) -> int: ...

    def set_favorite(self, user_id: str, content_id: str, favorited: bool) -> bool: ...

    def is_favorited(self, user_id: str, content_id: str) -> bool: ...

    def favorite_count(self, content_id: str) -> int: ...

    def list_favorites(self, user_id: str) -> list[str]: ...


@runtime_checkable
class ConversationRepository(Protocol):
    def create_conversation(self, user_id: str, title: str | None) -> dict[str, Any]: ...

    def get_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any] | None: ...

    def list_conversations(self, user_id: str) -> list[dict[str, Any]]: ...

    def append_message(self, user_id: str, conversation_id: str, message: dict[str, Any]) -> dict[str, Any]: ...

    def list_messages(self, user_id: str, conversation_id: str) -> list[dict[str, Any]]: ...


@runtime_checkable
class PetRepository(Protocol):
    """宠物的隐藏好感度。界面上不展示，只用来调节模型说话的口气。"""

    def get_affection(self, user_id: str) -> int: ...

    def add_affection(self, user_id: str, delta: int) -> int: ...


def conversation_timestamp_fields() -> tuple[str, ...]:
    return ("created_at", "updated_at")


def assert_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("expected datetime")
    return value
