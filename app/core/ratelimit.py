"""极简滑动窗口限流。

模拟期默认阈值宽松（600 次/分钟），仅用于让 RATE_LIMITED 分支可达；
真正的限流策略在上游适配器接入时按上游配额重新设计。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 60.0


class SlidingWindowLimiter:
    def __init__(self, window_seconds: float = WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def retry_after(self, key: str, limit: int) -> int:
        """允许通过返回 0；被限流返回建议等待秒数。limit<=0 表示关闭限流。"""
        if limit <= 0:
            return 0
        moment = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            while bucket and moment - bucket[0] > self._window:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, int(self._window - (moment - bucket[0])) + 1)
            bucket.append(moment)
            return 0

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()
