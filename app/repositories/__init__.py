"""仓储装配：按配置在内存实现与 MySQL 实现之间切换。

业务代码统一 `from app.repositories import store`。这里给的是一个**代理**，每次属性
访问都转发给当前生效的实现，所以换存储不需要改任何调用点；测试里注入/重置也只需
操作 `get_store()` / `reset_store()`。

选择规则：设了 `DATABASE_URL` 就走 MySQL，没设就走内存（模拟期默认）。驱动是延迟
导入的 —— 不接库的运行环境不需要装 pymysql。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from app.repositories.memory import store as memory_store

DATABASE_URL_ENV = "DATABASE_URL"

_lock = threading.Lock()
_active: Any = None


def database_url() -> str:
    return os.getenv(DATABASE_URL_ENV, "").strip()


def build_store() -> Any:
    if not database_url():
        return memory_store
    # 延迟导入：不接数据库的运行环境不需要安装 MySQL 驱动。
    from app.repositories.mysql.store import MySqlStore

    return MySqlStore(database_url())


def get_store() -> Any:
    global _active
    if _active is None:
        with _lock:
            if _active is None:
                _active = build_store()
    return _active


def reset_store() -> None:
    """丢弃当前实现，下次访问按配置重建（测试与切换配置用）。"""
    global _active
    with _lock:
        close = getattr(_active, "close", None)
        if callable(close):
            close()
        _active = None


class _StoreProxy:
    """把属性访问转发给当前实现，调用点因此不必关心具体存储。"""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_store(), name)

    def __repr__(self) -> str:
        return f"<store proxy -> {type(get_store()).__name__}>"


store = _StoreProxy()
