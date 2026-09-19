"""MySQL 仓储骨架（P1）。

接入方式：设置 DATABASE_URL 后由 app/main.py 的 get_store() 切换到本模块实现；
未配置时自动回落到内存实现，业务层无感。
"""

from __future__ import annotations

import os

DATABASE_URL = os.getenv("DATABASE_URL", "")


def is_configured() -> bool:
    return bool(DATABASE_URL.strip())


class MySqlStore:  # pragma: no cover - P1 阶段实现
    """占位实现：接口与 app/repositories/base.py 的 Protocol 一致。"""

    def __init__(self, url: str) -> None:
        if not is_configured():
            raise RuntimeError("DATABASE_URL 未配置，使用内存仓储")
        self.url = url
        raise NotImplementedError("MySQL 仓储属 P1 阶段范围")
