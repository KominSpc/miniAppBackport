"""MySQL 仓储。

设置 `DATABASE_URL` 后由 `app/repositories/__init__.py` 的 `get_store()` 切到这里；
未配置时自动回落到内存实现，业务层无感。表结构见 `schema.sql`，实现见 `store.py`。
"""

from __future__ import annotations

import os

from app.repositories.mysql.store import MySqlStore

DATABASE_URL_ENV = "DATABASE_URL"

__all__ = ["MySqlStore", "configured_url", "is_configured"]


def configured_url() -> str:
    return os.getenv(DATABASE_URL_ENV, "").strip()


def is_configured() -> bool:
    return bool(configured_url())
