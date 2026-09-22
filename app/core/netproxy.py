# -*- coding: utf-8 -*-
"""网络代理解析：显式配置优先，否则跟随操作系统设好的代理。

仓库里**不写死**代理地址，理由有三条：

1. 正式形态是安卓端，那里没有系统代理，走的是用户自己的网络；
2. 本机调试时用户自己会开代理（Clash / SakuraCat 之类会把代理写进 Windows 的
   「Internet 设置」），服务端跟着用就行，不必再维护第二份配置；
3. 写死在 .env 或启动脚本里的代理会和用户的网络环境打架——用户关掉代理后
   服务端还在往一个没人监听的端口发请求。

注意 httpx 只认 ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` 这几种环境变量，
**不会**读 Windows 注册表；``urllib.request.getproxies()`` 会。所以统一用后者探测。

回环地址永远直连：后端、音乐、漫画三个服务都跑在本机，绕一圈代理只会更慢，
甚至在代理没开时直接把本地调用打死。
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit
from urllib.request import getproxies

LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", ""})

# 只有这些上游需要代理：pixiv 及其图床在境内不可直连。其余上游（网易云 / QQ 音乐的
# CDN、漫画图床、漫画站）都是国内服务，走代理只会更慢，遇到代理客户端的全局模式还会
# 直接失败 —— 所以系统代理只对下面的后缀生效，其余一律直连。
PROXIED_SUFFIXES = frozenset(
    {
        "pixiv.net",
        "pximg.net",
        "pixiv.me",
        "fanbox.cc",
    }
)

# 系统代理变化不频繁，缓存一小会儿，避免每张图片都去读一次注册表。
_CACHE_SECONDS = 30.0
_cached_at = 0.0
_cached: str | None = None


def is_local_host(host: str) -> bool:
    """回环 / 本机地址（含 ``127.0.0.0/8``）。"""
    value = (host or "").strip().lower().strip("[]")
    if value in LOCAL_HOSTS:
        return True
    return value.startswith("127.")


def system_proxy(*, refresh: bool = False) -> str | None:
    """操作系统配置的代理地址；没有就返回 None。"""
    global _cached, _cached_at
    now = time.monotonic()
    if not refresh and now - _cached_at < _CACHE_SECONDS:
        return _cached
    resolved: str | None = None
    try:
        proxies = getproxies()
    except Exception:  # noqa: BLE001 - 探测失败就按直连处理
        proxies = {}
    for scheme in ("https", "http"):
        value = str(proxies.get(scheme) or "").strip()
        if value:
            resolved = value
            break
    _cached = resolved
    _cached_at = now
    return resolved


def proxy_for(url: str | None, explicit: str = "") -> str | None:
    """给 [url] 选代理：显式配置 > 系统代理（仅限代理上游）> 直连。

    [url] 为空表示「这个客户端的目标地址不固定」，按显式配置 / 系统代理来判；
    能拿到地址时：回环一律直连，国内上游也一律直连（见 [PROXIED_SUFFIXES]）。
    """
    host = (urlsplit(url).hostname or "") if url is not None else ""
    if is_local_host(host):
        return None
    value = (explicit or "").strip()
    if value:
        return value
    if host and not needs_proxy(host):
        return None
    return system_proxy()


def needs_proxy(host: str) -> bool:
    """主机是否属于「必须走代理」的上游（见 [PROXIED_SUFFIXES]）。"""
    value = (host or "").strip().lower().strip("[]")
    if not value:
        return False
    return any(value == suffix or value.endswith("." + suffix) for suffix in PROXIED_SUFFIXES)
