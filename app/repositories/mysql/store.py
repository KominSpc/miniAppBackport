"""MySQL 仓储实现。

与 `app/repositories/memory.py` 同接口（见 `app/repositories/base.py` 的 Protocol），
业务层只依赖 Protocol：配置了 `DATABASE_URL` 就切到这里，否则仍是内存实现。

与内存实现**刻意**保留的唯一差异：`create_user` 遇到已存在的 `install_id` 时复用原
用户（轮换令牌 + 刷新有效期），而不是另建一个。内存版在令牌过期后会重建记录，收藏
会无声地落到新账号上；这里保持同一 `user_id`，收藏因此能跨「令牌过期」存活。

时区：写库一律转 UTC，读回转 Asia/Shanghai（见 schema.sql 顶部的约定）。

连接：FastAPI 的同步接口跑在线程池里，所以按线程各持一条连接，失效自动重连。
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pymysql
from pymysql.cursors import DictCursor

from app.core.timeutil import SHANGHAI, iso, now
from app.constants.llm import AFFECTION_MAX, AFFECTION_MIN
from app.repositories.memory import DEFAULT_MUSIC_PLAYLIST_NAME, new_id, new_token

UTC = timezone.utc

"""每个用户最多保留多少条历史（与内存实现一致）。"""
HISTORY_KEEP = 200

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_PREFERENCES: dict[str, Any] = {
    "tags": [],
    "platforms": [],
    "genres": [],
    "safe_mode": True,
}


# ------------------------------------------------------------------ 时间


def _to_db(moment: datetime) -> datetime:
    """写入前的归一化：带时区的一律转 UTC 无时区；契约的 now() 只精确到秒。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SHANGHAI)
    return moment.astimezone(UTC).replace(tzinfo=None, microsecond=0)


def _from_db(moment: datetime | None) -> datetime | None:
    """读回后转成契约时区（Asia/Shanghai），与内存实现返回的对象一致。"""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(SHANGHAI).replace(microsecond=0)


# ------------------------------------------------------------------ JSON 编解码


def _json_encode(value: Any) -> Any:
    """快照里可能嵌着 datetime（ContentItem / MusicTrack 的时间字段），落库前拍成 ISO。"""
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_encode(item) for item in value]
    return value


def _json_decode(value: Any, key: str = "") -> Any:
    """把 `*_at` 上的 ISO 串还原成 datetime，让调用方拿到的形状与内存实现一致。"""
    if isinstance(value, dict):
        return {name: _json_decode(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_json_decode(item) for item in value]
    if isinstance(value, str) and key.endswith("_at") and "T" in value:
        return _parse_iso(value)
    return value


def _parse_iso(text: str) -> Any:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def dump_json(value: Any) -> str:
    return json.dumps(_json_encode(value), ensure_ascii=False)


def load_json(value: Any) -> Any:
    """MySQL 的 JSON 列由驱动原样返回字符串，这里统一解析并还原时间字段。"""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    return _json_decode(value)


# ------------------------------------------------------------------ 连接


def parse_url(url: str) -> dict[str, Any]:
    """`mysql://user:pass@host:3306/db?charset=utf8mb4` → 连接参数。"""
    raw = url.strip()
    for prefix in ("mysql+pymysql://", "mysql://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    parsed = urlparse("mysql://" + raw)
    database = parsed.path.lstrip("/")
    if not parsed.hostname or not database:
        raise ValueError("DATABASE_URL 需要形如 mysql://user:pass@host:3306/dbname")
    query = parse_qs(parsed.query)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
        "charset": (query.get("charset") or ["utf8mb4"])[0],
    }


def schema_statements(sql: str) -> list[str]:
    """把 schema.sql 拆成可逐条执行的语句（去掉注释与空行）。"""
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    body = "\n".join(lines)
    return [statement.strip() for statement in body.split(";") if statement.strip()]


class MySqlStore:
    """`app/repositories/base.py` 三个 Protocol 的 MySQL 实现。"""

    def __init__(self, url: str, *, ensure_schema: bool = True) -> None:
        self.url = url
        self._params = parse_url(url)
        self._local = threading.local()
        if ensure_schema:
            self.ensure_schema()

    # --- 连接管理 ---

    def ensure_schema(self) -> None:
        """建表（幂等）。表结构见同目录的 schema.sql。"""
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        for statement in schema_statements(sql):
            self._execute(statement)

    def _connect(self) -> pymysql.connections.Connection:
        params = self._params
        return pymysql.connect(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params["password"],
            database=params["database"],
            charset=params["charset"],
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=5,
            read_timeout=20,
            write_timeout=20,
        )

    def _conn(self) -> pymysql.connections.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                # 不带 reconnect 参数：pymysql 1.2 起该参数已废弃。探活失败就换一条
                # 新连接，效果一样。
                conn.ping()
                return conn
            except pymysql.MySQLError:
                try:
                    conn.close()
                except pymysql.MySQLError:
                    pass
        conn = self._connect()
        self._local.conn = conn
        return conn

    def close(self) -> None:
        """放掉本线程的连接（测试与配置切换时用）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._conn().cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._conn().cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount

    def _scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        row = self._one(sql, params)
        if row is None:
            return None
        return next(iter(row.values()))

    # --- 用户 ---

    @staticmethod
    def _user_record(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": row["user_id"],
            "install_id": row["install_id"],
            "platform": row["platform"],
            "app_version": row["app_version"],
            "created_at": _from_db(row["created_at"]),
            "preferences": load_json(row["preferences"]),
            "access_token": row["access_token"],
            "expires_at": _from_db(row["expires_at"]),
        }

    def create_user(
        self,
        *,
        install_id: str | None = None,
        platform: str | None = None,
        app_version: str | None = None,
        ttl_hours: int = 720,
        preferences: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if install_id:
            existing = self.find_by_install_id(install_id)
            if existing is not None:
                # 同一台设备的安装标识：复用原用户，收藏不会因为令牌过期而丢。
                return self.rotate_token(existing["user_id"], ttl_hours)
        moment = now()
        record = {
            "user_id": new_id("user"),
            "install_id": install_id,
            "platform": platform,
            "app_version": app_version,
            "created_at": moment,
            "expires_at": moment + timedelta(hours=ttl_hours),
            "preferences": dict(preferences or DEFAULT_PREFERENCES),
            "access_token": new_token(),
        }
        try:
            self._execute(
                "INSERT INTO anon_user"
                " (user_id, install_id, platform, app_version, access_token, expires_at, created_at, preferences)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record["user_id"],
                    install_id,
                    platform,
                    app_version,
                    record["access_token"],
                    _to_db(record["expires_at"]),
                    _to_db(record["created_at"]),
                    dump_json(record["preferences"]),
                ),
            )
        except pymysql.err.IntegrityError:
            # 并发下同一个 install_id 刚被别的线程建出来：改成复用。
            existing = self.find_by_install_id(install_id) if install_id else None
            if existing is None:
                raise
            return self.rotate_token(existing["user_id"], ttl_hours)
        return record

    def find_by_install_id(self, install_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM anon_user WHERE install_id = %s ORDER BY created_at ASC LIMIT 1",
            (install_id,),
        )
        return self._user_record(row) if row else None

    def rotate_token(self, user_id: str, ttl_hours: int) -> dict[str, Any]:
        expires = now() + timedelta(hours=ttl_hours)
        self._execute(
            "UPDATE anon_user SET access_token = %s, expires_at = %s WHERE user_id = %s",
            (new_token(), _to_db(expires), user_id),
        )
        record = self.get_user(user_id)
        if record is None:
            raise KeyError(user_id)
        return record

    def resolve_token(self, token: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM anon_user WHERE access_token = %s", (token,))
        return self._user_record(row) if row else None

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM anon_user WHERE user_id = %s", (user_id,))
        return self._user_record(row) if row else None

    def update_preferences(self, user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        row = self._one("SELECT preferences FROM anon_user WHERE user_id = %s", (user_id,))
        if row is None:
            raise KeyError(user_id)
        prefs = dict(load_json(row["preferences"]) or {})
        for key, value in patch.items():
            if value is not None:
                prefs[key] = value
        self._execute(
            "UPDATE anon_user SET preferences = %s WHERE user_id = %s",
            (dump_json(prefs), user_id),
        )
        return prefs

    # --- 历史 ---

    def append_history(self, user_id: str, entry: dict[str, Any]) -> None:
        moment = entry.get("created_at") or now()
        self._execute(
            "INSERT INTO history_entry (entry_id, user_id, kind, query_text, payload, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                entry.get("id") or new_id("hist"),
                user_id,
                entry.get("kind") or "browse",
                entry.get("query"),
                dump_json(entry),
                _to_db(moment),
            ),
        )
        # 只留最近 HISTORY_KEEP 条：MySQL 不允许在删除的子查询里直接引用同一张表，
        # 所以套一层派生表。
        self._execute(
            "DELETE FROM history_entry WHERE user_id = %s AND seq NOT IN ("
            " SELECT seq FROM (SELECT seq FROM history_entry WHERE user_id = %s"
            " ORDER BY seq DESC LIMIT %s) AS recent)",
            (user_id, user_id, HISTORY_KEEP),
        )

    def list_history(self, user_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            rows = self._query(
                "SELECT payload FROM history_entry WHERE user_id = %s ORDER BY seq DESC",
                (user_id,),
            )
        else:
            rows = self._query(
                "SELECT payload FROM history_entry WHERE user_id = %s AND kind = %s"
                " ORDER BY seq DESC",
                (user_id, kind),
            )
        return [load_json(row["payload"]) for row in rows]

    def delete_history(self, user_id: str, kind: str | None = None) -> int:
        if kind is None:
            return self._execute("DELETE FROM history_entry WHERE user_id = %s", (user_id,))
        return self._execute(
            "DELETE FROM history_entry WHERE user_id = %s AND kind = %s", (user_id, kind)
        )

    # --- 收藏（幂等） ---

    def set_favorite(
        self,
        user_id: str,
        content_id: str,
        favorited: bool,
        item: dict[str, Any] | None = None,
    ) -> bool:
        moment = _to_db(now())
        if favorited:
            self._execute(
                "INSERT IGNORE INTO favorite (user_id, content_id, created_at) VALUES (%s, %s, %s)",
                (user_id, content_id, moment),
            )
            if item is not None:
                # 重复收藏只换快照，seq 不动 —— 收藏列表的顺序因此保持稳定。
                self._execute(
                    "INSERT INTO favorite_snapshot (user_id, content_id, payload, created_at)"
                    " VALUES (%s, %s, %s, %s)"
                    " ON DUPLICATE KEY UPDATE payload = VALUES(payload)",
                    (user_id, content_id, dump_json(item), moment),
                )
        else:
            self._execute(
                "DELETE FROM favorite WHERE user_id = %s AND content_id = %s",
                (user_id, content_id),
            )
            self._execute(
                "DELETE FROM favorite_snapshot WHERE user_id = %s AND content_id = %s",
                (user_id, content_id),
            )
        return self.is_favorited(user_id, content_id)

    def is_favorited(self, user_id: str, content_id: str) -> bool:
        row = self._one(
            "SELECT 1 AS hit FROM favorite WHERE user_id = %s AND content_id = %s LIMIT 1",
            (user_id, content_id),
        )
        return row is not None

    def favorite_count(self, content_id: str) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM favorite WHERE content_id = %s", (content_id,)) or 0)

    def favorite_snapshots(self, user_id: str) -> dict[str, dict[str, Any]]:
        rows = self._query(
            "SELECT content_id, payload FROM favorite_snapshot WHERE user_id = %s ORDER BY seq ASC",
            (user_id,),
        )
        return {row["content_id"]: load_json(row["payload"]) for row in rows}

    def list_favorites(self, user_id: str) -> list[str]:
        rows = self._query(
            "SELECT content_id FROM favorite WHERE user_id = %s ORDER BY content_id ASC",
            (user_id,),
        )
        return [row["content_id"] for row in rows]

    # --- 音乐歌单（收藏夹） ---

    @staticmethod
    def _playlist_view(row: dict[str, Any], track_ids: list[str]) -> dict[str, Any]:
        return {
            "id": row["playlist_id"],
            "name": row["name"],
            "is_default": row["default_flag"] == 1,
            "created_at": _from_db(row["created_at"]),
            "track_count": len(track_ids),
            "track_ids": list(track_ids),
        }

    def _owns_playlist(self, user_id: str, playlist_id: str) -> bool:
        row = self._one(
            "SELECT 1 AS hit FROM music_playlist WHERE user_id = %s AND playlist_id = %s LIMIT 1",
            (user_id, playlist_id),
        )
        return row is not None

    def _track_in_playlist(self, playlist_id: str, track_id: str) -> bool:
        row = self._one(
            "SELECT 1 AS hit FROM music_playlist_item WHERE playlist_id = %s AND track_id = %s LIMIT 1",
            (playlist_id, track_id),
        )
        return row is not None

    def _has_track(self, user_id: str, track_id: str) -> bool:
        row = self._one(
            "SELECT 1 AS hit FROM music_playlist_item i"
            " JOIN music_playlist p ON p.playlist_id = i.playlist_id"
            " WHERE p.user_id = %s AND i.track_id = %s LIMIT 1",
            (user_id, track_id),
        )
        return row is not None

    def _track_ids_by_playlist(self, user_id: str) -> dict[str, list[str]]:
        rows = self._query(
            "SELECT i.playlist_id, i.track_id FROM music_playlist_item i"
            " JOIN music_playlist p ON p.playlist_id = i.playlist_id"
            " WHERE p.user_id = %s ORDER BY i.seq ASC",
            (user_id,),
        )
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["playlist_id"], []).append(row["track_id"])
        return grouped

    def list_music_playlists(self, user_id: str) -> list[dict[str, Any]]:
        """按创建顺序返回，默认歌单永远排第一（与内存实现一致）。"""
        rows = self._query(
            "SELECT * FROM music_playlist WHERE user_id = %s"
            " ORDER BY (default_flag IS NULL) ASC, seq ASC",
            (user_id,),
        )
        grouped = self._track_ids_by_playlist(user_id)
        return [self._playlist_view(row, grouped.get(row["playlist_id"], [])) for row in rows]

    def get_music_playlist(self, user_id: str, playlist_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM music_playlist WHERE user_id = %s AND playlist_id = %s",
            (user_id, playlist_id),
        )
        if row is None:
            return None
        ids = [
            item["track_id"]
            for item in self._query(
                "SELECT track_id FROM music_playlist_item WHERE playlist_id = %s ORDER BY seq ASC",
                (playlist_id,),
            )
        ]
        return self._playlist_view(row, ids)

    def create_music_playlist(self, user_id: str, name: str) -> dict[str, Any]:
        record = {"id": new_id("pl"), "name": name, "is_default": False, "created_at": now()}
        self._execute(
            "INSERT INTO music_playlist (playlist_id, user_id, name, default_flag, created_at)"
            " VALUES (%s, %s, %s, NULL, %s)",
            (record["id"], user_id, name, _to_db(record["created_at"])),
        )
        return {**record, "track_count": 0, "track_ids": []}

    def delete_music_playlist(self, user_id: str, playlist_id: str) -> bool:
        """默认歌单不允许删除（返回 False），否则旧版收藏夹接口会失去落点。"""
        row = self._one(
            "SELECT default_flag FROM music_playlist WHERE user_id = %s AND playlist_id = %s",
            (user_id, playlist_id),
        )
        if row is None or row["default_flag"] == 1:
            return False
        self._execute("DELETE FROM music_playlist_item WHERE playlist_id = %s", (playlist_id,))
        self._execute(
            "DELETE FROM music_playlist WHERE user_id = %s AND playlist_id = %s",
            (user_id, playlist_id),
        )
        return True

    def default_music_playlist_id(self, user_id: str) -> str:
        """默认歌单：不存在就建一个，保证「快速收藏」永远有落点。"""
        row = self._one(
            "SELECT playlist_id FROM music_playlist WHERE user_id = %s AND default_flag = 1 LIMIT 1",
            (user_id,),
        )
        if row is not None:
            return row["playlist_id"]
        playlist_id = new_id("pl")
        try:
            self._execute(
                "INSERT INTO music_playlist (playlist_id, user_id, name, default_flag, created_at)"
                " VALUES (%s, %s, %s, 1, %s)",
                (playlist_id, user_id, DEFAULT_MUSIC_PLAYLIST_NAME, _to_db(now())),
            )
        except pymysql.err.IntegrityError:
            # 并发下另一个线程刚建好默认歌单：直接用它的。
            row = self._one(
                "SELECT playlist_id FROM music_playlist WHERE user_id = %s AND default_flag = 1 LIMIT 1",
                (user_id,),
            )
            if row is None:
                raise
            return row["playlist_id"]
        return playlist_id

    def music_playlist_tracks(
        self, user_id: str, playlist_id: str
    ) -> dict[str, dict[str, Any]]:
        """歌单里的曲目快照，按加入先后返回。歌单不属于该用户时返回空表。"""
        if not self._owns_playlist(user_id, playlist_id):
            return {}
        rows = self._query(
            "SELECT track_id, payload FROM music_playlist_item WHERE playlist_id = %s ORDER BY seq ASC",
            (playlist_id,),
        )
        return {row["track_id"]: load_json(row["payload"]) for row in rows}

    def set_music_playlist_track(
        self,
        user_id: str,
        playlist_id: str,
        track_id: str,
        member: bool,
        snapshot: dict[str, Any] | None = None,
    ) -> bool:
        if not self._owns_playlist(user_id, playlist_id):
            return False
        if member:
            payload = snapshot if snapshot is not None else {"id": track_id}
            self._execute(
                "INSERT INTO music_playlist_item (playlist_id, track_id, payload, created_at)"
                " VALUES (%s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE payload = VALUES(payload)",
                (playlist_id, track_id, dump_json(payload), _to_db(now())),
            )
        else:
            self._execute(
                "DELETE FROM music_playlist_item WHERE playlist_id = %s AND track_id = %s",
                (playlist_id, track_id),
            )
        return self._track_in_playlist(playlist_id, track_id)

    def set_music_favorite(
        self,
        user_id: str,
        track_id: str,
        favorited: bool,
        snapshot: dict[str, Any] | None = None,
    ) -> bool:
        """旧版单收藏夹语义：收藏进默认歌单；取消收藏从**所有**歌单里移除。"""
        if favorited:
            playlist_id = self.default_music_playlist_id(user_id)
            payload = snapshot if snapshot is not None else {"id": track_id}
            self._execute(
                "INSERT INTO music_playlist_item (playlist_id, track_id, payload, created_at)"
                " VALUES (%s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE payload = VALUES(payload)",
                (playlist_id, track_id, dump_json(payload), _to_db(now())),
            )
        else:
            self._execute(
                "DELETE i FROM music_playlist_item i"
                " JOIN music_playlist p ON p.playlist_id = i.playlist_id"
                " WHERE p.user_id = %s AND i.track_id = %s",
                (user_id, track_id),
            )
        return self._has_track(user_id, track_id)

    def list_music_favorites(self, user_id: str) -> dict[str, dict[str, Any]]:
        """所有歌单的并集快照：只要收进任意一个歌单，就算「已收藏」。"""
        rows = self._query(
            "SELECT i.track_id, i.payload FROM music_playlist_item i"
            " JOIN music_playlist p ON p.playlist_id = i.playlist_id"
            " WHERE p.user_id = %s ORDER BY i.seq ASC",
            (user_id,),
        )
        return {row["track_id"]: load_json(row["payload"]) for row in rows}

    # --- 会话 ---

    _CONVERSATION_SELECT = (
        "SELECT c.*, (SELECT COUNT(*) FROM chat_message m"
        " WHERE m.conversation_id = c.conversation_id) AS message_count FROM conversation c"
    )

    @staticmethod
    def _conversation_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["conversation_id"],
            "title": row["title"],
            "created_at": _from_db(row["created_at"]),
            "updated_at": _from_db(row["updated_at"]),
            "message_count": int(row["message_count"] or 0),
        }

    @staticmethod
    def _message_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["message_id"],
            "role": row["role"],
            "content": row["content"],
            "created_at": _from_db(row["created_at"]),
            "expert": row["expert"],
            "live2d_action": row["live2d_action"],
            "suggestions": load_json(row["suggestions"]) or [],
        }

    def create_conversation(self, user_id: str, title: str | None = None) -> dict[str, Any]:
        moment = now()
        conversation_id = new_id("conv")
        self._execute(
            "INSERT INTO conversation (conversation_id, user_id, title, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (conversation_id, user_id, title, _to_db(moment), _to_db(moment)),
        )
        return {
            "id": conversation_id,
            "title": title,
            "created_at": moment,
            "updated_at": moment,
            "message_count": 0,
        }

    def get_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        row = self._one(
            self._CONVERSATION_SELECT
            + " WHERE c.user_id = %s AND c.conversation_id = %s",
            (user_id, conversation_id),
        )
        return self._conversation_view(row) if row else None

    def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            self._CONVERSATION_SELECT + " WHERE c.user_id = %s ORDER BY c.seq DESC",
            (user_id,),
        )
        return [self._conversation_view(row) for row in rows]

    def append_message(
        self, user_id: str, conversation_id: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        moment = message.get("created_at") or now()
        self._execute(
            "INSERT INTO chat_message"
            " (message_id, conversation_id, user_id, role, content, expert, live2d_action, suggestions, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                message.get("id") or new_id("msg"),
                conversation_id,
                user_id,
                message.get("role") or "user",
                message.get("content") or "",
                message.get("expert"),
                message.get("live2d_action"),
                dump_json(message.get("suggestions") or []),
                _to_db(moment),
            ),
        )
        self._execute(
            "UPDATE conversation SET updated_at = %s WHERE conversation_id = %s",
            (_to_db(moment), conversation_id),
        )
        return message

    def list_messages(self, user_id: str, conversation_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT m.* FROM chat_message m"
            " JOIN conversation c ON c.conversation_id = m.conversation_id"
            " WHERE c.user_id = %s AND m.conversation_id = %s ORDER BY m.seq ASC",
            (user_id, conversation_id),
        )
        return [self._message_view(row) for row in rows]

    # --- 宠物好感度 ---

    def get_affection(self, user_id: str) -> int:
        value = self._scalar("SELECT score FROM pet_affection WHERE user_id = %s", (user_id,))
        return int(value or 0)

    def add_affection(self, user_id: str, delta: int) -> int:
        """累加好感度并夹在 [AFFECTION_MIN, AFFECTION_MAX]；返回累加后的值。

        夹值放在 SQL 里（LEAST/GREATEST）而不是先读后写：同一用户并发两轮对话时
        不会互相覆盖。`VALUES(score)` 是 5.7 / 8.0 都认的写法（8.0.20 起标记为
        废弃，但线上那台 5.7 只有它可用）。
        """
        if not delta:
            return self.get_affection(user_id)
        # 插入分支的 ON DUPLICATE KEY 不会执行，夹值必须在这里先做一遍，
        # 否则第一条记录能写进 999 这种越界值（实测踩过）。
        # 下界取 -AFFECTION_MAX 而不是 AFFECTION_MIN：越界的负增量应该把分数压到
        # 下限（100 + (-999) → 0），而不是被夹成 0 变成「什么都不做」，
        # 这样才和 MemoryStore 的「夹最终分数」语义一致。
        inserted = max(-AFFECTION_MAX, min(AFFECTION_MAX, int(delta)))
        self._execute(
            "INSERT INTO pet_affection (user_id, score, updated_at) VALUES (%s, %s, %s)"
            " ON DUPLICATE KEY UPDATE"
            " score = LEAST(GREATEST(score + VALUES(score), %s), %s),"
            " updated_at = VALUES(updated_at)",
            (user_id, inserted, _to_db(now()), AFFECTION_MIN, AFFECTION_MAX),
        )
        return self.get_affection(user_id)

    # --- 生命周期 ---

    def reset(self) -> dict[str, int]:
        """清空全部用户数据（`POST /v1/dev/reset`）。返回清理前的条数。"""
        counts = {
            "users": int(self._scalar("SELECT COUNT(*) FROM anon_user") or 0),
            "favorites": int(self._scalar("SELECT COUNT(*) FROM favorite") or 0),
            "history": int(self._scalar("SELECT COUNT(*) FROM history_entry") or 0),
            "conversations": int(self._scalar("SELECT COUNT(*) FROM conversation") or 0),
        }
        for table in (
            "anon_user",
            "favorite",
            "favorite_snapshot",
            "history_entry",
            "music_playlist",
            "music_playlist_item",
            "conversation",
            "chat_message",
            "pet_affection",
        ):
            self._execute(f"TRUNCATE TABLE {table}")
        return counts
