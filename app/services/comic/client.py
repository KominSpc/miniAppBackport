"""漫画上游 HTTP 客户端。

上游是仓库同级的 `ComicDown` 爬虫库，由 `mini_app/tools/comic_service.py` 包成一个
Flask 服务（默认 `127.0.0.1:19631`）。每个站点的 `search / latest / tags / list /
comic / chapter` 都被统一成同样的 JSON 形状，所以这里只要一个「GET JSON」+「GET 图片」。

实测结论（决定这里的实现方式，见 docs/EXECUTION_PLAN.md 的漫画小节）：

1. 三个可用站点是 `manhuagui`（漫画柜）/ `qq`（腾讯动漫）/ `dm5`（动漫屋）；
2. **内页图有防盗链**：`i.hamreus.com`（manhuagui 的图床）不带
   `Referer: https://www.manhuagui.com/` 必定 403；腾讯与动漫屋带不带都能取。
   所以图片统一由本服务转发，并按域名决定要不要补 Referer（见 `_image_headers`）；
3. manhuagui 的内页地址带签名（`?e=&m=`）且会过期，因此**章节目录不长期缓存**，
   每次阅读都重新取一次。
4. dm5 的部分条目是「系列总页」，能搜到但章节为空 —— 详情里照实返回 0 章，
   由界面提示「换一部或换来源」，不在这一层猜。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.core import netproxy
from app.core.errors import AppError

logger = logging.getLogger("miniappbackport.comic")

MAX_BACKOFF_SECONDS = 10.0
JITTER_SECONDS = 0.3

#: 图片回源的**连接**超时。
#:
#: 图床都是多 IP 主机（``cf.mhgui.com`` 走 Cloudflare 两个 IP，``i.hamreus.com`` 三个），
#: httpcore 按顺序逐个拨号、每个地址吃满连接超时；碰到连不上的那个 IP，一张图就要
#: 白等几十秒。压到 3.5 秒可以快速换下一个地址 / 下一次重试；连通后连接池复用，
#: 后续都是 0.0-0.4 秒。读超时另给 45 秒：长条内页单张可达 1-2MB。
IMAGE_CONNECT_TIMEOUT_SECONDS = 3.5


class _ImageLease:
    """一次图片请求的租约：读完调用 close()，把连接还给连接池。

    连接池本身（[_ImageLease] 不负责）由 [ComicClient.close] 统一回收。
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    def close(self) -> None:
        self._response.close()


class ComicClient:
    """最小可用的漫画客户端：GET JSON 与 GET 图片。"""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client
        # 图片直连用的常驻连接池（与 JSON 池分开：图片会占住连接若干秒）。
        # 为什么常驻：一屏漫画内页有十几二十张，每张都新建客户端要重做
        # DNS + TCP + TLS，冷连接实测 1-9 秒，复用连接后是 0.0-0.4 秒。
        self._image_client: httpx.Client | None = None
        self._image_proxy: str | None = None

    # ------------------------------------------------------------------ 配置

    @property
    def configured(self) -> bool:
        """是否配置了上游地址。没配时不发请求，接口返回 503。"""
        return bool(self._settings.comic_api_base.strip()) and self._settings.comic_enabled

    @property
    def user_agent(self) -> str:
        return self._settings.comic_user_agent

    def transport(self) -> httpx.Client:
        """JSON 用的客户端：只连本机漫画服务，因此显式关掉代理（见 [netproxy]）。

        ``trust_env=False`` 是必须的：``urllib.request.getproxies()`` 只要在环境里
        见到任何 ``*_proxy`` 变量（本仓库就有 ``PIXIV_PROXY`` / ``BILIBILI_PROXY``）
        就不再回落到 Windows 注册表的系统代理，而 httpx 又只认 http/https/all 这几种
        scheme —— 结果是「本来有代理」的进程被静默改成直连（见 docs 漫画小节）。
        """
        if self._client is None:
            self._client = httpx.Client(
                timeout=self._settings.comic_timeout_seconds,
                follow_redirects=True,
                trust_env=False,
            )
        return self._client

    def close(self) -> None:
        self.drop_image_transport()
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------ 图片连接池

    def _image_transport(self, proxy: str | None) -> httpx.Client:
        """图片直连客户端。代理变了就重建（本仓库里漫画图床永远是直连）。"""
        if self._client is not None:
            return self._client
        if self._image_client is None or self._image_proxy != proxy:
            self.drop_image_transport()
            self._image_client = httpx.Client(
                timeout=httpx.Timeout(
                    IMAGE_CONNECT_TIMEOUT_SECONDS, read=45.0, write=15.0, pool=5.0
                ),
                follow_redirects=True,
                trust_env=False,
                proxy=proxy,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
            self._image_proxy = proxy
        return self._image_client

    def drop_image_transport(self) -> None:
        """丢掉图片连接池：keep-alive 里可能留着对端已经关闭的连接。"""
        if self._image_client is not None:
            self._image_client.close()
            self._image_client = None
            self._image_proxy = None

    # ------------------------------------------------------------------ JSON

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise AppError(
                "UPSTREAM_UNAVAILABLE", details={"reason": "comic_service_not_configured"}
            )
        url = f"{self._settings.comic_api_base}{path}"
        response = self._request(url, params=params)
        try:
            payload = response.json()
        except ValueError as error:  # 上游返回 HTML（多半是打到了别的服务）
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "invalid_json"}) from error
        if not isinstance(payload, dict):
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "invalid_envelope"})
        # 爬虫自己的 404 形状：{"error": "NOT_FOUND", "message": "..."}
        if payload.get("error"):
            raise AppError("NOT_FOUND", details={"reason": str(payload.get("error"))})
        return payload

    # ------------------------------------------------------------------ 图片

    def image_allowed(self, url: str) -> bool:
        """只允许回源到漫画图床，避免这个接口变成任意地址的代理。"""
        hosts = self._settings.comic_image_hosts
        if not hosts:
            return True
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return False
        return any(host == suffix or host.endswith("." + suffix) for suffix in hosts)

    def referer_for(self, url: str) -> str | None:
        """按域名给 Referer：manhuagui 的图床不带就 403，其余站点带了反而可能被挡。"""
        host = (urlsplit(url).hostname or "").lower()
        for suffix, referer in self._settings.comic_image_referers:
            if host == suffix or host.endswith("." + suffix):
                return referer
        return None

    def _image_headers(self, url: str) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        referer = self.referer_for(url)
        if referer:
            headers["Referer"] = referer
        return headers

    def open_image(
        self, url: str, *, range_header: str | None = None
    ) -> tuple[_ImageLease, httpx.Response]:
        """打开一张漫画图片（调用方负责关闭）。"""
        if not self.image_allowed(url):
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "unexpected_image_host"})
        headers = self._image_headers(url)
        if range_header:
            headers["Range"] = range_header
        # 图片走的是外部图床（不是本机的漫画服务）。代理按 [netproxy] 解析：
        # 显式配置 > 系统代理 > 直连；回环地址永远直连。
        #
        # trust_env=False 是刻意的：httpx 只认 HTTP_PROXY 这类环境变量，而本仓库的
        # 环境里恰好有 PIXIV_PROXY / COMIC_PROXY 这种「长得像但不是」的变量，
        # 交给它自己判断反而会静默改成直连。代理一律由这里显式传入。
        #
        # **失败重试一次**：图床抖动（连接被对端掐断 / 半关闭的 keep-alive 连接）
        # 是「漫画图片有时加载不出来」的主要来源之一，重试一次基本自愈；重试仍失败
        # 才把错误抛给上层，由前端展示占位与重试按钮。
        client = None
        response = None
        for attempt in (0, 1):
            proxy = netproxy.proxy_for(url, self._settings.comic_proxy)
            client = self._image_transport(proxy)
            try:
                request = client.build_request("GET", url, headers=headers)
                response = client.send(request, stream=True)
                break
            except httpx.HTTPError as error:
                # 复用连接池时第一次可能拿到对端已关闭的 keep-alive 连接：
                # 丢掉池子重试一次，仍然失败才报错。
                self.drop_image_transport()
                if attempt == 0:
                    logger.warning("漫画图片回源失败（第 1 次，重试）：%r", error)
                    time.sleep(0.2)
                    continue
                logger.warning("漫画图片回源失败：%r", error)
                raise AppError(
                    "UPSTREAM_UNAVAILABLE",
                    details={
                        "reason": type(error).__name__,
                        "detail": str(error)[:160],
                        "proxy": self._settings.comic_proxy or "none",
                    },
                ) from error
        assert client is not None and response is not None
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": f"image_http_{status}"})
        return _ImageLease(response), response

    # ------------------------------------------------------------------ 内部

    def _request(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        client = self.transport()
        attempts = max(1, self._settings.comic_max_retries + 1)
        backoff = max(0.1, self._settings.comic_retry_initial_seconds)
        last_reason = "unknown"

        for attempt in range(attempts):
            try:
                response = client.request("GET", url, params=params)
            except httpx.TimeoutException as error:
                # 超时不重试：站点被墙/维护时是「一直挂」，重试只是把 8 秒变 16 秒，
                # 而调用方（搜索）还要接着试下一个站点。5xx / 连接错误才值得再试一次。
                raise AppError(
                    "UPSTREAM_UNAVAILABLE",
                    details={
                        "reason": type(error).__name__,
                        "timeout": self._settings.comic_timeout_seconds,
                    },
                ) from error
            except httpx.HTTPError as error:
                last_reason = type(error).__name__
                logger.warning("漫画上游请求失败（第 %s 次）：%s", attempt + 1, last_reason)
            else:
                status = response.status_code
                if status == 404:
                    raise AppError("NOT_FOUND", details={"source": "comic"})
                if status >= 500:
                    last_reason = f"http_{status}"
                elif status >= 400:
                    raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": f"http_{status}"})
                else:
                    return response

            if attempt < attempts - 1:
                time.sleep(backoff + random.uniform(0, JITTER_SECONDS))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": last_reason})
