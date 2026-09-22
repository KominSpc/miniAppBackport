# -*- coding: utf-8 -*-
"""进程内字节缓存：LRU + TTL + 单飞（single-flight）。

给「封面 / 漫画内页」这类**同源代理**接口用。同一张图在一次会话里会被反复请求
（上下滚动再滚回来、退出阅读器又进来、两端同看同一部），每次都回源不仅慢，
还会把上游的反爬/限流招来。实测回源单张封面 0.4–4.2 秒、内页最大 12 秒，
命中缓存后是 0 毫秒。

单飞是必须的：一屏 20 张封面同时到达时，同一个 key 只真正回源一次，其余请求
在这一把键锁上排队，等第一个填完缓存直接读。没有它，并发重复请求等于把触发
上游限流的概率乘以并发数。

缓存按**总字节数**封顶，超了按 LRU 淘汰；条目数也设上限，避免大量小图把
字典撑爆。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

_MISSING = object()


class ByteCache:
    """线程安全的字节缓存；值统一是 ``(bytes, content_type)``。"""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_entries: int = 512,
        ttl_seconds: float = 1800.0,
    ) -> None:
        self._max_bytes = max(1, int(max_bytes))
        self._max_entries = max(1, int(max_entries))
        self._ttl = max(1.0, float(ttl_seconds))
        self._lock = threading.Lock()
        # key -> (到期时刻 monotonic, 值, 值占的字节数)
        self._entries: OrderedDict[str, tuple[float, tuple[bytes, str], int]] = OrderedDict()
        self._size = 0
        self._key_locks: dict[str, threading.Lock] = {}

    # ---------------------------------------------------------------- 读写

    def get(self, key: str) -> tuple[bytes, str] | None:
        """取缓存；过期自动丢弃。"""
        now = time.monotonic()
        with self._lock:
            found = self._entries.get(key)
            if found is None:
                return None
            expires_at, value, size = found
            if now > expires_at:
                self._entries.pop(key, None)
                self._size -= size
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: str, value: tuple[bytes, str]) -> None:
        body = value[0]
        size = len(body) + len(value[1])
        if size > self._max_bytes:
            # 单张就超预算（超大原图）：不缓存，免得把别的全挤掉。
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._size -= previous[2]
            self._entries[key] = (time.monotonic() + self._ttl, value, size)
            self._size += size
            while self._entries and (
                self._size > self._max_bytes or len(self._entries) > self._max_entries
            ):
                _, (_, _, removed_size) = self._entries.popitem(last=False)
                self._size -= removed_size

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._size = 0

    def load(self, key: str, loader: Callable[[], tuple[bytes, str]]) -> tuple[bytes, str]:
        """缓存命中直接返回，否则回源一次并写入（同一 key 并发时只回源一次）。"""
        cached = self.get(key)
        if cached is not None:
            return cached
        with self._key_lock(key):
            # 排队期间别人可能已经填好了
            cached = self.get(key)
            if cached is not None:
                return cached
            value = loader()
            self.put(key, value)
            return value

    # ---------------------------------------------------------------- 内部

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            found = self._key_locks.get(key)
            if found is None:
                found = threading.Lock()
                self._key_locks[key] = found
            return found
