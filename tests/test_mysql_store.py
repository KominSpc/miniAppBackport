"""MySQL 仓储的集成测试。

默认**跳过**，只有显式设置了 `TEST_DATABASE_URL` 才跑：

    $env:TEST_DATABASE_URL = "mysql://root:密码@127.0.0.1:3306/miniapp_test"
    pytest tests/test_mysql_store.py

刻意不复用 `DATABASE_URL`：这里的 fixture 会 **TRUNCATE 全部业务表**，用一个单独的
变量，才不会有人手一抖把正在调试的库清空。表名/字符集与线上一致。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

pytest.importorskip("pymysql")

from app.core.timeutil import SHANGHAI, now
from app.repositories.mysql.store import MySqlStore

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL", "").strip(),
    reason="未设置 TEST_DATABASE_URL，跳过 MySQL 集成测试",
)

SNAPSHOT = {
    "id": "img_0001",
    "published_at": datetime(2026, 9, 21, 20, 30, tzinfo=SHANGHAI),
    "payload": {"author": "画师", "created_at": datetime(2026, 9, 20, 8, 0, tzinfo=SHANGHAI)},
}
TRACK = {
    "id": "ne_347230",
    "title": "海阔天空",
    "created_at": datetime(2026, 9, 21, 9, 0, tzinfo=SHANGHAI),
}


@pytest.fixture
def mysql_store() -> MySqlStore:
    store = MySqlStore(os.environ["TEST_DATABASE_URL"])
    store.reset()
    yield store
    store.reset()
    store.close()


@pytest.fixture
def user(mysql_store: MySqlStore) -> dict:
    return mysql_store.create_user(
        install_id="install-1", platform="windows", app_version="1.0.0", ttl_hours=720
    )


def test_create_user_and_resolve_token(mysql_store: MySqlStore, user: dict) -> None:
    assert user["user_id"].startswith("user_")
    found = mysql_store.resolve_token(user["access_token"])
    assert found is not None
    assert found["user_id"] == user["user_id"]
    assert found["preferences"] == {
        "tags": [],
        "platforms": [],
        "genres": [],
        "safe_mode": True,
    }
    # 时间读回来必须是带时区的（deps 要拿它和 now() 比大小）
    assert found["expires_at"].utcoffset() is not None
    assert found["expires_at"] > now()
    assert abs(found["created_at"] - now()) < timedelta(minutes=1)


def test_same_install_id_reuses_the_user(mysql_store: MySqlStore, user: dict) -> None:
    """同一台设备再注册：复用原用户，收藏不会因为令牌过期而丢到新账号上。"""
    again = mysql_store.create_user(
        install_id="install-1", platform="android", app_version="1.0.1", ttl_hours=720
    )
    assert again["user_id"] == user["user_id"]
    # 令牌轮换：旧令牌立即失效
    assert again["access_token"] != user["access_token"]
    assert mysql_store.resolve_token(user["access_token"]) is None
    assert mysql_store.resolve_token(again["access_token"]) is not None


def test_rotate_token_refreshes_expiry(mysql_store: MySqlStore, user: dict) -> None:
    rotated = mysql_store.rotate_token(user["user_id"], ttl_hours=1)
    assert rotated["expires_at"] < user["expires_at"]
    assert rotated["access_token"] != user["access_token"]


def test_update_preferences_ignores_none(mysql_store: MySqlStore, user: dict) -> None:
    prefs = mysql_store.update_preferences(
        user["user_id"], {"safe_mode": False, "tags": None}
    )
    assert prefs["safe_mode"] is False
    assert prefs["tags"] == []
    assert mysql_store.get_user(user["user_id"])["preferences"]["safe_mode"] is False


def test_favorite_is_idempotent_and_keeps_snapshot_order(
    mysql_store: MySqlStore, user: dict
) -> None:
    uid = user["user_id"]
    assert mysql_store.set_favorite(uid, "img_0001", True, SNAPSHOT) is True
    assert mysql_store.set_favorite(uid, "img_0001", True, SNAPSHOT) is True
    assert mysql_store.set_favorite(uid, "img_0002", True, {**SNAPSHOT, "id": "img_0002"}) is True
    assert mysql_store.favorite_count("img_0001") == 1
    assert mysql_store.list_favorites(uid) == ["img_0001", "img_0002"]

    snapshots = mysql_store.favorite_snapshots(uid)
    assert list(snapshots) == ["img_0001", "img_0002"]
    # 快照里的 datetime 必须活着回来
    assert snapshots["img_0001"]["published_at"] == SNAPSHOT["published_at"]
    assert snapshots["img_0001"]["payload"]["author"] == "画师"

    assert mysql_store.set_favorite(uid, "img_0001", False) is False
    assert mysql_store.favorite_count("img_0001") == 0
    assert "img_0001" not in mysql_store.favorite_snapshots(uid)


def test_favorite_count_spans_users(mysql_store: MySqlStore, user: dict) -> None:
    other = mysql_store.create_user(install_id="install-2", platform="android")
    mysql_store.set_favorite(user["user_id"], "img_9", True)
    mysql_store.set_favorite(other["user_id"], "img_9", True)
    assert mysql_store.favorite_count("img_9") == 2
    assert mysql_store.list_favorites(user["user_id"]) == ["img_9"]


def test_history_keeps_newest_first_and_filters_by_kind(
    mysql_store: MySqlStore, user: dict
) -> None:
    uid = user["user_id"]
    mysql_store.append_history(
        uid, {"id": "hist_1", "kind": "search", "query": "初音", "content": None,
              "created_at": now()}
    )
    mysql_store.append_history(
        uid, {"id": "hist_2", "kind": "browse", "query": None, "content": SNAPSHOT,
              "created_at": now()}
    )
    entries = mysql_store.list_history(uid, None)
    assert [item["id"] for item in entries] == ["hist_2", "hist_1"]
    # content 里的时间也要还原
    assert entries[0]["content"]["published_at"] == SNAPSHOT["published_at"]
    assert [item["id"] for item in mysql_store.list_history(uid, "search")] == ["hist_1"]
    assert mysql_store.delete_history(uid, "search") == 1
    assert [item["id"] for item in mysql_store.list_history(uid, None)] == ["hist_2"]


def test_history_is_capped_per_user(mysql_store: MySqlStore, user: dict) -> None:
    uid = user["user_id"]
    for index in range(205):
        mysql_store.append_history(
            uid, {"id": f"hist_{index}", "kind": "search", "query": f"q{index}",
                  "content": None, "created_at": now()}
        )
    entries = mysql_store.list_history(uid, None)
    assert len(entries) == 200
    assert entries[0]["id"] == "hist_204"


def test_default_playlist_is_created_once_and_cannot_be_deleted(
    mysql_store: MySqlStore, user: dict
) -> None:
    uid = user["user_id"]
    first = mysql_store.default_music_playlist_id(uid)
    assert mysql_store.default_music_playlist_id(uid) == first
    assert mysql_store.delete_music_playlist(uid, first) is False
    views = mysql_store.list_music_playlists(uid)
    assert len(views) == 1
    assert views[0]["is_default"] is True
    assert views[0]["name"] == "我喜欢的音乐"


def test_playlists_keep_creation_order_with_default_first(
    mysql_store: MySqlStore, user: dict
) -> None:
    uid = user["user_id"]
    mine = mysql_store.create_music_playlist(uid, "我的歌单")
    later = mysql_store.create_music_playlist(uid, "通勤")
    default = mysql_store.default_music_playlist_id(uid)
    views = mysql_store.list_music_playlists(uid)
    assert [item["id"] for item in views] == [default, mine["id"], later["id"]]
    assert mysql_store.delete_music_playlist(uid, mine["id"]) is True
    assert [item["id"] for item in mysql_store.list_music_playlists(uid)] == [
        default,
        later["id"],
    ]


def test_playlist_tracks_keep_insertion_order(
    mysql_store: MySqlStore, user: dict
) -> None:
    uid = user["user_id"]
    playlist = mysql_store.create_music_playlist(uid, "我的歌单")
    mysql_store.set_music_playlist_track(uid, playlist["id"], "ne_1", True, TRACK)
    mysql_store.set_music_playlist_track(uid, playlist["id"], "ne_2", True, {**TRACK, "id": "ne_2"})
    tracks = mysql_store.music_playlist_tracks(uid, playlist["id"])
    assert list(tracks) == ["ne_1", "ne_2"]
    assert tracks["ne_1"]["created_at"] == TRACK["created_at"]
    assert mysql_store.get_music_playlist(uid, playlist["id"])["track_ids"] == ["ne_1", "ne_2"]
    assert mysql_store.set_music_playlist_track(uid, playlist["id"], "ne_1", False) is False
    assert list(mysql_store.music_playlist_tracks(uid, playlist["id"])) == ["ne_2"]


def test_music_favorite_uses_default_playlist_and_removes_from_all(
    mysql_store: MySqlStore, user: dict
) -> None:
    uid = user["user_id"]
    assert mysql_store.set_music_favorite(uid, "ne_347230", True, TRACK) is True
    default = mysql_store.default_music_playlist_id(uid)
    assert mysql_store.music_playlist_tracks(uid, default) == {"ne_347230": TRACK}

    other = mysql_store.create_music_playlist(uid, "另一个歌单")
    mysql_store.set_music_playlist_track(uid, other["id"], "ne_347230", True, TRACK)
    # 取消收藏要把这首歌从**所有**歌单里摘掉
    assert mysql_store.set_music_favorite(uid, "ne_347230", False) is False
    assert mysql_store.music_playlist_tracks(uid, default) == {}
    assert mysql_store.music_playlist_tracks(uid, other["id"]) == {}
    assert mysql_store.list_music_favorites(uid) == {}


def test_music_favorites_union_across_playlists(
    mysql_store: MySqlStore, user: dict
) -> None:
    uid = user["user_id"]
    playlist = mysql_store.create_music_playlist(uid, "我的歌单")
    mysql_store.set_music_favorite(uid, "ne_1", True, {**TRACK, "id": "ne_1"})
    mysql_store.set_music_playlist_track(uid, playlist["id"], "ne_2", True, {**TRACK, "id": "ne_2"})
    assert set(mysql_store.list_music_favorites(uid)) == {"ne_1", "ne_2"}


def test_playlist_of_another_user_is_not_touchable(
    mysql_store: MySqlStore, user: dict
) -> None:
    """别的用户的歌单 ID 写不进去（内存实现没这道闸，SQL 里必须有）。"""
    other = mysql_store.create_user(install_id="install-9", platform="android")
    theirs = mysql_store.create_music_playlist(other["user_id"], "他的歌单")
    assert mysql_store.set_music_playlist_track(
        user["user_id"], theirs["id"], "ne_1", True, TRACK
    ) is False
    assert mysql_store.music_playlist_tracks(user["user_id"], theirs["id"]) == {}
    assert mysql_store.get_music_playlist(user["user_id"], theirs["id"]) is None


def test_conversation_and_messages_round_trip(
    mysql_store: MySqlStore, user: dict
) -> None:
    uid = user["user_id"]
    conversation = mysql_store.create_conversation(uid, title="开场白")
    assert conversation["message_count"] == 0
    moment = now()
    mysql_store.append_message(
        uid, conversation["id"],
        {"id": "msg_1", "role": "user", "content": "在吗", "created_at": moment,
         "expert": None, "live2d_action": None, "suggestions": []},
    )
    mysql_store.append_message(
        uid, conversation["id"],
        {"id": "msg_2", "role": "assistant", "content": "在的", "created_at": moment,
         "expert": "anime", "live2d_action": "happy",
         "suggestions": [{"id": "img_0001", "published_at": SNAPSHOT["published_at"]}]},
    )
    messages = mysql_store.list_messages(uid, conversation["id"])
    assert [item["id"] for item in messages] == ["msg_1", "msg_2"]
    assert messages[1]["expert"] == "anime"
    assert messages[1]["live2d_action"] == "happy"
    # 推荐位里的时间字段同样要还原成 datetime
    assert messages[1]["suggestions"][0]["published_at"] == SNAPSHOT["published_at"]

    listed = mysql_store.list_conversations(uid)
    assert [item["id"] for item in listed] == [conversation["id"]]
    assert listed[0]["message_count"] == 2
    assert listed[0]["updated_at"] == moment


def test_another_users_conversation_is_hidden(mysql_store: MySqlStore, user: dict) -> None:
    other = mysql_store.create_user(install_id="install-8", platform="android")
    theirs = mysql_store.create_conversation(other["user_id"], title="私聊")
    assert mysql_store.get_conversation(user["user_id"], theirs["id"]) is None
    assert mysql_store.list_messages(user["user_id"], theirs["id"]) == []


def test_reset_reports_counts_and_clears(mysql_store: MySqlStore, user: dict) -> None:
    uid = user["user_id"]
    mysql_store.set_favorite(uid, "img_1", True, SNAPSHOT)
    mysql_store.append_history(
        uid, {"id": "hist_1", "kind": "search", "query": "q", "content": None, "created_at": now()}
    )
    mysql_store.create_conversation(uid, title="t")
    counts = mysql_store.reset()
    assert counts == {"users": 1, "favorites": 1, "history": 1, "conversations": 1}
    assert mysql_store.list_favorites(uid) == []
    assert mysql_store.list_history(uid, None) == []
    assert mysql_store.list_conversations(uid) == []


# --------------------------------------------------------------- 隐藏好感度


def test_affection_starts_at_zero_and_accumulates(mysql_store: MySqlStore, user: dict) -> None:
    uid = user["user_id"]

    assert mysql_store.get_affection(uid) == 0
    assert mysql_store.add_affection(uid, 3) == 3
    # 第二次是累加而不是覆盖（ON DUPLICATE KEY UPDATE 走的是 score + VALUES(score)）
    assert mysql_store.add_affection(uid, 2) == 5


def test_affection_is_clamped_and_isolated_per_user(
    mysql_store: MySqlStore, user: dict
) -> None:
    uid = user["user_id"]
    other = mysql_store.create_user(
        install_id="install-2", platform="desktop", app_version="1.0.0", ttl_hours=720
    )

    assert mysql_store.add_affection(uid, 999) == 100
    assert mysql_store.add_affection(uid, -999) == 0
    # 只加 0 时不该建记录，也不该报错
    assert mysql_store.add_affection(uid, 0) == 0
    assert mysql_store.get_affection(other["user_id"]) == 0


def test_affection_survives_a_new_store_instance(mysql_store: MySqlStore, user: dict) -> None:
    """换一个进程/连接读同一个库：好感度必须还在（这正是它落库的理由）。"""
    uid = user["user_id"]
    mysql_store.add_affection(uid, 7)
    mysql_store.close()

    again = MySqlStore(os.environ["TEST_DATABASE_URL"])
    try:
        assert again.get_affection(uid) == 7
    finally:
        again.close()
