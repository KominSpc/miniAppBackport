"""MySQL 仓储里不依赖数据库的那部分：URL 解析、建表语句拆分、JSON 时间往返。

这些用例**永远会跑**（不需要数据库），因为快照里的 datetime 一旦在 JSON 里走丢，
收藏列表和聊天记录的时间就会退化成字符串，问题很隐蔽。
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("pymysql")

from app.core.timeutil import SHANGHAI
from app.repositories.mysql.store import (
    _from_db,
    _to_db,
    dump_json,
    load_json,
    parse_url,
    schema_statements,
)

MOMENT = datetime(2026, 9, 21, 20, 30, 0, tzinfo=SHANGHAI)


def test_parse_url_reads_all_parts() -> None:
    params = parse_url("mysql://miniapp:pw%401@127.0.0.1:3307/miniapp?charset=utf8mb4")
    assert params["host"] == "127.0.0.1"
    assert params["port"] == 3307
    assert params["user"] == "miniapp"
    # 密码里的 @ 会被 URL 编码，必须解回来，否则登录必然失败
    assert params["password"] == "pw@1"
    assert params["database"] == "miniapp"
    assert params["charset"] == "utf8mb4"


def test_parse_url_accepts_sqlalchemy_prefix_and_defaults() -> None:
    params = parse_url("mysql+pymysql://root:pw@localhost/miniapp")
    assert params["host"] == "localhost"
    assert params["port"] == 3306
    assert params["charset"] == "utf8mb4"


def test_parse_url_rejects_incomplete_url() -> None:
    with pytest.raises(ValueError):
        parse_url("mysql://root:pw@127.0.0.1:3306")


def test_schema_statements_skips_comments() -> None:
    sql = "-- 注释\nCREATE TABLE a (id INT);\n\n-- 又一行注释\nCREATE TABLE b (id INT);\n"
    statements = schema_statements(sql)
    assert len(statements) == 2
    assert all("--" not in item for item in statements)
    assert statements[0].startswith("CREATE TABLE a")


def test_json_round_trip_restores_datetime() -> None:
    """收藏快照里嵌着 ContentItem 的时间字段，写库再读回必须还是 datetime。"""
    snapshot = {
        "id": "img_0001",
        "published_at": MOMENT,
        "payload": {"created_at": MOMENT, "author": "画师"},
        "tags": [{"updated_at": MOMENT}],
    }
    restored = load_json(dump_json(snapshot))
    assert restored["published_at"] == MOMENT
    assert restored["payload"]["created_at"] == MOMENT
    assert restored["tags"][0]["updated_at"] == MOMENT
    # 中文不能被转义成 \uXXXX
    assert "画师" in dump_json(snapshot)


def test_json_round_trip_keeps_plain_strings_and_numbers() -> None:
    restored = load_json(dump_json({"title": "2026-09-21", "count": 3, "flag": True}))
    # 不以 _at 结尾的键即使长得像日期也不动它
    assert restored == {"title": "2026-09-21", "count": 3, "flag": True}


def test_db_time_round_trip_keeps_shanghai_offset() -> None:
    stored = _to_db(MOMENT)
    # 落库是 UTC 的无时区值（20:30 +08:00 → 12:30 UTC）
    assert stored.tzinfo is None
    assert stored.hour == 12
    assert _from_db(stored) == MOMENT
    assert _from_db(stored).utcoffset() is not None
