"""音频流地址的短期签名。

**为什么需要它**：音频最终由播放器内核取字节，而两边都拿不到 ``Authorization``：

- web：``<audio>`` / ``just_audio_web`` 用媒体元素播放，浏览器禁止脚本给媒体元素加请求头；
- 安卓：ExoPlayer 只有走 ``setUrl(headers=)`` 才带得上，且一旦换用系统播放器 / 外部
  应用打开就彻底失效。

于是把「鉴权」从请求头挪到 URL 上：``/playback``（需要 Bearer）返回带签名的流地址，
``/stream`` 只校验签名。签名短期有效，并且和曲目 ID 绑定，因此不会变成公开的无鉴权
代理；上游直链本身也只活约 20 分钟（``expi``），签名 TTL 与之同量级。
"""

from __future__ import annotations

import hashlib
import hmac
import time

#: 签名长度足够抗碰撞，又不至于把 URL 撑得太长。
SIGNATURE_LENGTH = 32


def signature_for(track_id: str, expires_at: int, secret: str) -> str:
    """对「曲目 ID + 过期时刻」做 HMAC-SHA256。"""
    message = f"{track_id}:{expires_at}".encode()
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return digest[:SIGNATURE_LENGTH]


def expires_at(ttl_seconds: int, *, now: float | None = None) -> int:
    moment = time.time() if now is None else now
    return int(moment) + max(1, ttl_seconds)


def verify(
    track_id: str,
    expires_at_value: int | str | None,
    signature: str | None,
    secret: str,
    *,
    now: float | None = None,
) -> bool:
    """校验签名与有效期；任一不满足都算不通过。"""
    if not signature:
        return False
    try:
        stamp = int(expires_at_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    moment = time.time() if now is None else now
    if stamp < moment:
        return False
    expected = signature_for(track_id, stamp, secret)
    return hmac.compare_digest(expected, signature)


def signed_query(track_id: str, ttl_seconds: int, secret: str) -> str:
    """拼出 ``exp=...&sig=...``，调用方自行决定接到哪个地址后面。"""
    stamp = expires_at(ttl_seconds)
    return f"exp={stamp}&sig={signature_for(track_id, stamp, secret)}"
