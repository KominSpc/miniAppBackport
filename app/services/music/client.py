"""音乐上游 HTTP 客户端。

上游是仓库同级的 ``multiPlatformMusicApi``（Node/Express）：它把网易云与 QQ 音乐的
接口统一成 ``/{模块路径}?platform=...`` 的形式，成功响应顶层直出 ``code: 200``。

实测结论（决定这里的实现方式）：

1. ``/search``、``/song/detail``、``/lyric``、``/comment/hot``、``/top/song`` 全部
   **不需要登录**，也不需要代理，本机直连即可；
2. ``/song/url/v1`` 未登录时忽略 ``level``，恒返回同一份 ``br=128001`` 的 mp3；会员曲目
   （``fee != 0``）只给试听片段并带 ``freeTrialInfo``。因此调用侧默认请求最高档，然后
   以响应里的真实 ``level/br`` 为准，见 ``service.py``；
3. 音频地址是**带签名的临时直链**（``expi`` 约 1200 秒），所以不能缓存地址、不能把地址
   下发给客户端：客户端拿到的永远是本服务的 ``/v1/music/tracks/{id}/stream``。

``client`` 参数用于测试注入 ``httpx.Client(transport=...)``，不产生真实网络请求。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.core import netproxy
from app.core.errors import AppError

logger = logging.getLogger("miniappbackport.music")

MAX_BACKOFF_SECONDS = 20.0
JITTER_SECONDS = 0.5
UPSTREAM_OK = 200

#: 直连媒体（封面 / 音频）的**连接**超时。
#:
#: 实测：``p1/p3/p4.music.126.net`` 这类多 IP 主机在国内网络上经常「一个 IP 完全
#: 连不上、另一个 0.2-0.6 秒就通」。httpcore 是**按顺序**逐个地址拨号的，每个地址
#: 都吃满连接超时：按 ``music_timeout_seconds``（15 秒）算，一次坏 IP 就让一张 7KB 的
#: 封面白等 15-30 秒，前端只能看到「封面转圈」。压到 3.5 秒即可快速放弃坏 IP，
#: 由 ``open_media`` 的重试换到好 IP；连上以后连接池复用，后续都是 0.0-0.4 秒。
MEDIA_CONNECT_TIMEOUT_SECONDS = 3.5


class _MediaLease:
    """一次媒体请求的租约。

    调用方拿到的是「响应 + 可能自建的连接」：读完调用 ``close()``，自建的连接会被关掉，
    注入进来的共享客户端（测试用的 MockTransport）则保持存活。
    """

    def __init__(self, client: httpx.Client, response: httpx.Response, *, owned: bool) -> None:
        self._client = client
        self._response = response
        self._owned = owned

    def close(self) -> None:
        self._response.close()
        if self._owned:
            self._client.close()


class MusicClient:
    """最小可用的音乐客户端：GET JSON 与 GET 音频流。"""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client
        # 直连媒体（封面 / 音频）用的常驻连接池，与 JSON 池分开，见 _direct_media_transport。
        self._media_client: httpx.Client | None = None
        # 测试注入的客户端归调用方所有，不能在这里关掉；自建的要能随时重建。
        self._owns_client = client is None

    # ------------------------------------------------------------------ 配置

    @property
    def configured(self) -> bool:
        """是否配置了上游地址。没配时不发请求，接口返回 503。"""
        return bool(self._settings.music_api_base.strip())

    @property
    def default_platform(self) -> str:
        return self._settings.music_default_platform.strip().lower() or "netease"

    def transport(self) -> httpx.Client:
        if self._client is None:
            # JSON 只打本机的音乐服务（127.0.0.1），所以 trust_env=False：
            # 用户环境里有没有 HTTP_PROXY 都不该影响一次本机调用。
            self._client = httpx.Client(
                timeout=self._settings.music_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

    def close(self) -> None:
        self._drop_media_transport()
        if self._client is not None:
            self._client.close()
            self._client = None

    def _direct_media_transport(self) -> httpx.Client:
        """直连媒体（封面 / 音频）用的常驻客户端。

        为什么和 JSON 池分开：音频流会占住连接好几分钟，混在一起会把 JSON 请求饿死。
        为什么常驻：封面一屏要拉二三十张，每次新建客户端都要重做 DNS + TCP + TLS，
        实测首张之后每张还要 1 秒以上；复用连接后只有第一张付握手成本。
        """
        if self._media_client is None:
            self._media_client = httpx.Client(
                timeout=httpx.Timeout(MEDIA_CONNECT_TIMEOUT_SECONDS, read=60.0, write=15.0, pool=5.0),
                follow_redirects=True,
                trust_env=False,
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
            )
        return self._media_client

    def _drop_media_transport(self) -> None:
        """丢掉媒体连接池：keep-alive 里可能留着对端已经关闭的连接。"""
        if self._media_client is not None:
            self._media_client.close()
            self._media_client = None

    def _reset_transport(self) -> None:
        """丢弃连接池，下次请求重新建连；注入的测试客户端不动。"""
        if not self._owns_client:
            return
        self.close()

    # ------------------------------------------------------------------ JSON

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise AppError(
                "UPSTREAM_UNAVAILABLE", details={"reason": "music_service_not_configured"}
            )
        url = f"{self._settings.music_api_base}{path}"
        response = self._request(url, params=params)
        try:
            payload = response.json()
        except ValueError as error:  # 上游返回 HTML（多半是打到了别的服务）
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "invalid_json"}) from error
        if not isinstance(payload, dict):
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "invalid_envelope"})
        code = payload.get("code")
        if code is not None and code != UPSTREAM_OK:
            raise AppError(
                "UPSTREAM_UNAVAILABLE", details={"reason": "upstream_error", "code": code}
            )
        return payload

    # ------------------------------------------------------------------ 音频流

    def stream_allowed(self, url: str) -> bool:
        """只允许回源到配置里的媒体域名后缀，避免这个接口变成任意地址的代理。

        向上游要音频地址时实测主机名是 ``m701/m702/m801...music.126.net`` 这类
        **数字不固定**的 CDN 节点，逐条列举主机名会漏，因此这里按域名后缀匹配
        （``music.126.net`` 同时覆盖 ``m702.music.126.net``）。
        """
        hosts = self._settings.music_media_hosts
        if not hosts:
            return True
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return False
        return any(host == suffix or host.endswith("." + suffix) for suffix in hosts)

    def open_media(
        self, url: str, *, range_header: str | None = None
    ) -> tuple[_MediaLease, httpx.Response]:
        """打开音频流或封面图（调用方负责关闭）。转发 Range，客户端因此可以拖动进度条。"""
        if not self.stream_allowed(url):
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "unexpected_media_host"})
        headers = {
            "User-Agent": self._settings.music_user_agent,
            "Accept": "*/*",
            "Referer": self._settings.music_referer,
            # 明确要求不压缩：`/stream` 会把上游的 `content-length` 原样转发给
            # 播放器，而 httpx 的 `iter_bytes()` 会**自动解压**。上游一旦压了
            # （CDN 对部分节点会），转发出去的长度就和声明对不上，播放器只能
            # 认定这次加载失败 —— 表现就是「这首突然播不了，刷新又好」。
            "Accept-Encoding": "identity",
        }
        if range_header:
            headers["Range"] = range_header
        # 媒体流绝不复用 JSON 那个连接池：音频要占用连接几分钟（封面也一样），
        # 混在一起会把池子占满，JSON 请求只能干等；反过来 JSON 侧重建连接池
        # （见 _reset_transport）也会把正在播的流一起掐断。
        # 注入的客户端（测试）仍然直接复用，用完不关，统一交给 _MediaLease。
        # 音频 / 封面是外部 CDN（music.126.net）。netproxy 只把系统代理用在 pixiv
        # 这类必须代理的上游上，网易云 CDN 拿到的永远是 None（直连）。
        proxy = netproxy.proxy_for(url, "")
        client, owned = self._media_transport(proxy)
        for attempt in (0, 1):
            try:
                request = client.build_request("GET", url, headers=headers)
                response = client.send(request, stream=True)
                break
            except httpx.HTTPError as error:
                # 复用连接池时第一次可能拿到对端已关闭的 keep-alive 连接：
                # 丢掉池子重试一次，仍然失败才报错。
                if owned:
                    client.close()
                if attempt == 0 and not owned:
                    self._drop_media_transport()
                    client, owned = self._media_transport(proxy)
                    continue
                raise AppError(
                    "UPSTREAM_UNAVAILABLE", details={"reason": type(error).__name__}
                ) from error
        if response.status_code >= 400:
            response.close()
            if owned:
                client.close()
            raise AppError(
                "UPSTREAM_UNAVAILABLE",
                details={"reason": f"media_http_{response.status_code}"},
            )
        return _MediaLease(client, response, owned=owned), response

    def _media_transport(self, proxy: str | None) -> tuple[httpx.Client, bool]:
        """选媒体请求用的客户端：直连走常驻池，需要代理时为这一次请求临时建一个。

        返回值第二项表示「这个客户端是本次临时建的，用完要关」。
        """
        if not self._owns_client:
            # 测试注入的客户端归调用方所有，直接复用。
            return cast(httpx.Client, self._client), False
        if proxy is None:
            return self._direct_media_transport(), False
        return (
            httpx.Client(
                timeout=httpx.Timeout(self._settings.music_timeout_seconds, read=60.0),
                follow_redirects=True,
                trust_env=False,
                proxy=proxy,
            ),
            True,
        )

    # ------------------------------------------------------------------ 内部

    def _request(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        attempts = max(1, self._settings.music_max_retries + 1)
        backoff = max(0.1, self._settings.music_retry_initial_seconds)
        last_reason = "unknown"

        for attempt in range(attempts):
            try:
                response = self.transport().request("GET", url, params=params)
            except httpx.HTTPError as error:
                last_reason = type(error).__name__
                logger.warning("音乐上游请求失败（第 %s 次）：%s", attempt + 1, last_reason)
                # 连接级失败多半是连接池里留着半关闭的旧连接（上游重启过就会这样）。
                # 只重试不换连接池等于一直复用坏连接，所以这里直接把池子丢掉。
                self._reset_transport()
            else:
                status = response.status_code
                if status == 429:
                    wait = float(response.headers.get("retry-after") or backoff)
                    if attempt == attempts - 1:
                        raise AppError(
                            "UPSTREAM_UNAVAILABLE", details={"reason": "rate_limited"}
                        )
                    time.sleep(min(max(wait, 0.5), MAX_BACKOFF_SECONDS))
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue
                if status == 404:
                    raise AppError("NOT_FOUND", details={"source": "music"})
                if status >= 500:
                    last_reason = f"http_{status}"
                elif status >= 400:
                    raise AppError(
                        "UPSTREAM_UNAVAILABLE", details={"reason": f"http_{status}"}
                    )
                else:
                    return response

            if attempt < attempts - 1:
                time.sleep(backoff + random.uniform(0, JITTER_SECONDS))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": last_reason})
