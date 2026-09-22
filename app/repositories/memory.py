"""内存仓储：模拟期唯一实现。

进程内单例，`POST /v1/dev/reset` 会清空全部用户数据。
"""

from __future__ import annotations

import secrets
import threading
from datetime import timedelta
from typing import Any

from app.core.timeutil import iso, now
from app.constants.llm import AFFECTION_MAX, AFFECTION_MIN


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


DEFAULT_MUSIC_PLAYLIST_NAME = "我喜欢的音乐"


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
            self.favorite_items: dict[str, dict[str, dict[str, Any]]] = {}
            # 音乐歌单（收藏夹）：ID 前缀与图片收藏不同，混在一起会互相污染，因此
            # 单独命名空间。一个用户可有多个歌单；旧的单收藏夹接口落在默认歌单上
            # （见 set_music_favorite）。
            self.music_playlists: dict[str, dict[str, dict[str, Any]]] = {}
            self.music_playlist_items: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
            self.history: dict[str, list[dict[str, Any]]] = {}
            self.conversations: dict[str, dict[str, Any]] = {}
            self.messages: dict[str, list[dict[str, Any]]] = {}
            self.user_conversations: dict[str, list[str]] = {}
            # 宠物的隐藏好感度（用户 id → 分数）。界面上不显示，只影响模型口气。
            self.affection: dict[str, int] = {}
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

    def set_favorite(
        self,
        user_id: str,
        content_id: str,
        favorited: bool,
        item: dict[str, Any] | None = None,
    ) -> bool:
        """收藏 / 取消收藏（幂等）。

        ``item`` 为收藏那一刻的条目快照：收藏列表因此可以**只读内存**，不必逐条回源
        上游（见 ``app/api/v1/images.py`` 的 ``/favorites``）。取消收藏时快照一并丢弃。
        """
        with self._lock:
            bucket = self.favorites.setdefault(user_id, set())
            snapshots = self.favorite_items.setdefault(user_id, {})
            count = self.favorite_counts.get(content_id, 0)
            if favorited:
                if content_id not in bucket:
                    bucket.add(content_id)
                    count += 1
                if item is not None:
                    snapshots[content_id] = item
            else:
                if content_id in bucket:
                    bucket.discard(content_id)
                    count = max(0, count - 1)
                snapshots.pop(content_id, None)
            self.favorite_counts[content_id] = count
            return content_id in bucket

    def is_favorited(self, user_id: str, content_id: str) -> bool:
        with self._lock:
            return content_id in self.favorites.get(user_id, set())

    def favorite_count(self, content_id: str) -> int:
        with self._lock:
            return self.favorite_counts.get(content_id, 0)

    def favorite_snapshots(self, user_id: str) -> dict[str, dict[str, Any]]:
        """该用户的收藏条目快照，按收藏先后顺序返回（内存，进程重启即失效）。"""
        with self._lock:
            return dict(self.favorite_items.get(user_id, {}))

    def list_favorites(self, user_id: str) -> list[str]:
        with self._lock:
            return sorted(self.favorites.get(user_id, set()))

    # --- 音乐歌单（收藏夹，与图片收藏分开命名空间） ---

    @staticmethod
    def _music_playlist_view(
        record: dict[str, Any], tracks: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "id": record["id"],
            "name": record["name"],
            "is_default": record["is_default"],
            "created_at": record["created_at"],
            "track_count": len(tracks),
            "track_ids": list(tracks.keys()),
        }

    def list_music_playlists(self, user_id: str) -> list[dict[str, Any]]:
        """该用户的歌单，按创建顺序返回；默认歌单永远排第一（内存，进程重启即失效）。"""
        with self._lock:
            playlists = self.music_playlists.get(user_id, {})
            tracks = self.music_playlist_items.get(user_id, {})
            views = [
                self._music_playlist_view(record, tracks.get(playlist_id, {}))
                for playlist_id, record in playlists.items()
            ]
        return sorted(views, key=lambda item: 0 if item["is_default"] else 1)

    def get_music_playlist(self, user_id: str, playlist_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self.music_playlists.get(user_id, {}).get(playlist_id)
            if record is None:
                return None
            tracks = self.music_playlist_items.get(user_id, {}).get(playlist_id, {})
            return self._music_playlist_view(record, tracks)

    def create_music_playlist(self, user_id: str, name: str) -> dict[str, Any]:
        with self._lock:
            record = {
                "id": new_id("pl"),
                "name": name,
                "is_default": False,
                "created_at": now(),
            }
            self.music_playlists.setdefault(user_id, {})[record["id"]] = record
            self.music_playlist_items.setdefault(user_id, {})[record["id"]] = {}
            return self._music_playlist_view(record, {})

    def delete_music_playlist(self, user_id: str, playlist_id: str) -> bool:
        """删除歌单。默认歌单不允许删除（返回 False），否则旧版收藏夹接口会失去落点。"""
        with self._lock:
            record = self.music_playlists.get(user_id, {}).get(playlist_id)
            if record is None or record["is_default"]:
                return False
            self.music_playlists[user_id].pop(playlist_id, None)
            self.music_playlist_items.get(user_id, {}).pop(playlist_id, None)
            return True

    def default_music_playlist_id(self, user_id: str) -> str:
        """默认歌单：不存在就建一个，保证「快速收藏」永远有落点。"""
        with self._lock:
            playlists = self.music_playlists.setdefault(user_id, {})
            for record in playlists.values():
                if record["is_default"]:
                    return record["id"]
            record = {
                "id": new_id("pl"),
                "name": DEFAULT_MUSIC_PLAYLIST_NAME,
                "is_default": True,
                "created_at": now(),
            }
            playlists[record["id"]] = record
            self.music_playlist_items.setdefault(user_id, {})[record["id"]] = {}
            return record["id"]

    def music_playlist_tracks(self, user_id: str, playlist_id: str) -> dict[str, dict[str, Any]]:
        """歌单里的曲目快照，按加入先后返回。歌单不存在时返回空表。"""
        with self._lock:
            if playlist_id not in self.music_playlists.get(user_id, {}):
                return {}
            return dict(self.music_playlist_items.get(user_id, {}).get(playlist_id, {}))

    def set_music_playlist_track(
        self,
        user_id: str,
        playlist_id: str,
        track_id: str,
        member: bool,
        snapshot: dict[str, Any] | None = None,
    ) -> bool:
        """把曲目加入 / 移出歌单（幂等）。snapshot 是加入那一刻的曲目快照。"""
        with self._lock:
            bucket = self.music_playlist_items.setdefault(user_id, {}).setdefault(playlist_id, {})
            if member:
                bucket[track_id] = snapshot if snapshot is not None else {"id": track_id}
            else:
                bucket.pop(track_id, None)
            return track_id in bucket

    def set_music_favorite(
        self,
        user_id: str,
        track_id: str,
        favorited: bool,
        snapshot: dict[str, Any] | None = None,
    ) -> bool:
        """旧版单收藏夹语义：收藏进默认歌单；取消收藏从**所有**歌单里移除。

        列表页的 ♥ 就是这个语义——它表达的是一首歌「收没收藏过」，而不管收在哪。
        """
        with self._lock:
            if favorited:
                playlist_id = self.default_music_playlist_id(user_id)
                bucket = self.music_playlist_items.setdefault(user_id, {}).setdefault(
                    playlist_id, {}
                )
                bucket[track_id] = snapshot if snapshot is not None else {"id": track_id}
            else:
                for bucket in self.music_playlist_items.get(user_id, {}).values():
                    bucket.pop(track_id, None)
            return any(
                track_id in bucket
                for bucket in self.music_playlist_items.get(user_id, {}).values()
            )

    def list_music_favorites(self, user_id: str) -> dict[str, dict[str, Any]]:
        """所有歌单的并集快照：只要收进任意一个歌单，就算「已收藏」。"""
        with self._lock:
            merged: dict[str, dict[str, Any]] = {}
            for bucket in self.music_playlist_items.get(user_id, {}).values():
                merged.update(bucket)
            return merged

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

    # --- 宠物好感度 ---

    def get_affection(self, user_id: str) -> int:
        with self._lock:
            return int(self.affection.get(user_id, 0))

    def add_affection(self, user_id: str, delta: int) -> int:
        """累加好感度并夹在 [AFFECTION_MIN, AFFECTION_MAX]；返回累加后的值。"""
        with self._lock:
            value = int(self.affection.get(user_id, 0)) + int(delta)
            value = max(AFFECTION_MIN, min(AFFECTION_MAX, value))
            self.affection[user_id] = value
            return value


store = MemoryStore()


def format_timestamp(value: Any) -> str:
    return iso(value)
