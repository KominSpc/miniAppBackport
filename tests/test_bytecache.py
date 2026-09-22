"""字节缓存：TTL / LRU / 单飞。

这三点都是「图片代理」这条链路上必须成立的性质：TTL 决定站点换图后多久能看到，
LRU 决定内存不会被撑爆，单飞决定一屏 20 张封面同时到达时只回源一次。
"""

from __future__ import annotations

import threading
import time

from app.core.bytecache import ByteCache


def test_hit_returns_same_bytes_without_reloading() -> None:
    cache = ByteCache(max_bytes=1024, ttl_seconds=60)
    calls: list[int] = []

    def load() -> tuple[bytes, str]:
        calls.append(1)
        return b"body", "image/jpeg"

    first = cache.load("k", load)
    second = cache.load("k", load)
    assert first == second == (b"body", "image/jpeg")
    assert len(calls) == 1


def test_expired_entry_is_refetched() -> None:
    cache = ByteCache(max_bytes=1024, ttl_seconds=60)
    values = [b"old", b"new"]

    assert cache.load("k", lambda: (values[0], "image/jpeg"))[0] == b"old"
    # 把到期时刻往前挪，省得测试真的睡到 TTL 结束
    cache._entries["k"] = (time.monotonic() - 1, (values[0], "image/jpeg"), 0)
    assert cache.load("k", lambda: (values[1], "image/jpeg"))[0] == b"new"


def test_lru_evicts_until_under_budget() -> None:
    # 每条 100 字节 + content-type 长度，预算只装得下两条
    cache = ByteCache(max_bytes=240, max_entries=10, ttl_seconds=60)
    for index in range(3):
        cache.put(f"k{index}", (b"x" * 100, "image/jpeg"))
    assert cache.get("k0") is None, "最久未使用的应该先被淘汰"
    assert cache.get("k1") is not None
    assert cache.get("k2") is not None


def test_oversized_value_is_not_cached() -> None:
    cache = ByteCache(max_bytes=64, ttl_seconds=60)
    cache.put("big", (b"x" * 4096, "image/jpeg"))
    assert cache.get("big") is None


def test_entry_cap_is_enforced() -> None:
    cache = ByteCache(max_bytes=10 * 1024 * 1024, max_entries=3, ttl_seconds=60)
    for index in range(5):
        cache.put(f"k{index}", (b"x", "image/jpeg"))
    assert cache.get("k4") is not None
    assert cache.get("k0") is None


def test_concurrent_misses_only_load_once() -> None:
    """单飞：并发的同一个 key 只回源一次，其余等结果。"""
    cache = ByteCache(max_bytes=1024, ttl_seconds=60)
    calls: list[int] = []

    def load() -> tuple[bytes, str]:
        calls.append(1)
        time.sleep(0.05)
        return b"body", "image/jpeg"

    results: list[tuple[bytes, str]] = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.load("k", load)))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1
    assert results == [(b"body", "image/jpeg")] * 8


def test_failed_load_is_not_cached() -> None:
    """回源失败不能把失败缓存下来，否则站点恢复后还是拿不到图。"""
    cache = ByteCache(max_bytes=1024, ttl_seconds=60)

    def boom() -> tuple[bytes, str]:
        raise RuntimeError("upstream down")

    for _ in range(2):
        try:
            cache.load("k", boom)
        except RuntimeError:
            pass
    assert cache.get("k") is None
