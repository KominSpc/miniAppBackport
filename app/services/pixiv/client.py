"""Pixiv 上游 HTTP 客户端（服务端唯一的凭据出口）。

对应 ``docs/EXECUTION_PLAN.md`` 7.2 / 7.4 的实测结论：

1. **只读且单身份串行**：只发 GET；不轮换账号、不做 Cookie 池（违规且易封号）。
   命中 429 时读 ``Retry-After``，无该头则指数退避 + 随机抖动。
2. **凭据只在服务端**：Cookie 来自配置（``PIXIV_COOKIE`` 环境变量），任何响应与
   日志都不回显它。
3. **登录态失效统一映射**：401 / 403 / 3xx 跳登录页 → ``503 UPSTREAM_UNAVAILABLE``，
   不向客户端泄露上游细节（见 7.4 第 2 条）。
4. **排行榜用 JSON**：加 ``format=json`` 直接拿 JSON，不做 HTML 正则解析。

``client`` 参数用于测试注入 ``httpx.Client(transport=...)``，不产生真实网络请求。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from app.config import Settings
from app.core import netproxy
from app.core.errors import AppError, not_found, rate_limited

logger = logging.getLogger("miniappbackport.pixiv")

ACCEPT_JSON = "application/json, text/plain, */*"

# 退避上限与抖动：避免被判定为攻击流量
MAX_BACKOFF_SECONDS = 30.0
JITTER_SECONDS = 0.8


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


class PixivClient:
    """最小可用的 Pixiv 客户端：GET JSON 与 GET 字节流。"""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client
        # 注入的客户端（测试 / 别的调用方）归调用方所有，重连时不能把它关掉。
        self._owns_client = client is None

    # ------------------------------------------------------------------ 配置

    @property
    def configured(self) -> bool:
        """是否已提供登录态。未配置时核心接口仍可用，但推荐流会缺字段。"""
        return bool(self._settings.pixiv_cookie.strip())

    def headers(self, *, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self._settings.pixiv_user_agent,
            "Accept": ACCEPT_JSON,
            "Accept-Language": self._settings.pixiv_accept_language,
            "Referer": referer or self._settings.pixiv_referer,
        }
        cookie = self._settings.pixiv_cookie.strip()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def transport(self) -> httpx.Client:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "timeout": self._settings.pixiv_timeout_seconds,
                "follow_redirects": False,
            }
            # 显式配置优先；没配就跟系统代理走（本机开着 Clash 时自动生效，
            # 安卓端没有系统代理则直连）。仓库里不写死代理地址。
            proxy = netproxy.proxy_for(self._settings.pixiv_base_url, self._settings.pixiv_proxy)
            if proxy:
                kwargs["proxy"] = proxy
            # 打一行日志：出问题时能一眼看出「到底用没用上代理」，不用再去猜
            # 系统代理有没有被读到（代理地址本身不是密钥，可以打）。
            logger.info("pixiv 客户端就绪（代理：%s）", proxy or "无，直连")
            self._client = httpx.Client(**kwargs)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _reset_transport(self) -> None:
        """丢掉连接池，下次请求重新建连；注入的测试客户端不动。"""
        if not self._owns_client:
            return
        self.close()

    # ------------------------------------------------------------------ 请求

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """取 ``/ajax/...`` 接口；信封为 ``{error, message, body}``。"""
        response = self._request(path, params=params)
        try:
            payload = response.json()
        except ValueError as error:  # 上游返回 HTML（多半是被拦截或改版）
            raise AppError(
                "UPSTREAM_UNAVAILABLE", details={"reason": "invalid_json"}
            ) from error
        if not isinstance(payload, dict):
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "invalid_envelope"})
        if payload.get("error"):
            # 上游明确说「不存在」时按 404 透出，便于客户端区分
            message = str(payload.get("message") or "")
            if "not found" in message.lower() or "存在しません" in message:
                raise not_found({"source": "pixiv"})
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "upstream_error"})
        return payload

    def get_bytes(self, url: str) -> tuple[bytes, str]:
        """取图片字节。``i.pximg.net`` 强校验 Referer，必须带 pixiv 站点 Referer。"""
        response = self._request(url, referer=self._settings.pixiv_referer)
        content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return response.content, content_type or "image/jpeg"

    # ------------------------------------------------------------------ 内部

    def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
    ) -> httpx.Response:
        # 每次请求都显式带上请求头：注入的 httpx.Client（测试）因此不需要预先配置，
        # 也保证图片回源时 Referer 一定是我们自己的。
        headers = self.headers(referer=referer)
        # /ajax/... 这类站内路径补上站点前缀；pximg 的绝对地址原样使用
        if not url.startswith(("http://", "https://")):
            url = f"{self._settings.pixiv_base_url}{url}"
        attempts = max(1, self._settings.pixiv_max_retries + 1)
        backoff = max(0.1, self._settings.pixiv_retry_initial_seconds)
        last_reason = "unknown"
        # 是否已经为「换一条连接」多试过一次（只对超时生效，见下）
        timeout_retried = False

        for attempt in range(attempts):
            try:
                # 每轮都重新取一次客户端：上一轮可能刚把坏掉的连接池丢掉
                response = self.transport().request("GET", url, params=params, headers=headers)
            except httpx.HTTPError as error:
                last_reason = type(error).__name__
                logger.warning("pixiv 请求失败（第 %s 次）：%s", attempt + 1, last_reason)
                # 连接池里可能留着对端已经关掉的 keep-alive 连接（本机走代理时最常见：
                # 代理把连接握在手里，上游断了就是「干等」而不是 RST）。只重试、不换
                # 连接，等于一直复用同一条坏连接 —— 表现就是「pixiv 一直转圈，直到超时，
                # 之后每次也一样」。所以连接级失败一律先丢掉池子，下一轮重新建连。
                self._reset_transport()
                if isinstance(error, httpx.TimeoutException):
                    # 超时特别贵：默认 20 秒 × 4 次 = 80 秒，客户端（20 秒读超时）早就
                    # 放弃了，用户看到的是「作者页一直转圈」。只允许为「换一条连接」多试
                    # 一次；换过还超时，说明上游是真的不通，立刻返回错误，别再拖着等。
                    if timeout_retried:
                        break
                    timeout_retried = True
                    continue
            else:
                status = response.status_code
                if status == 429:
                    wait = _retry_after(response) or backoff
                    if attempt == attempts - 1:
                        raise rate_limited(int(max(1.0, wait)))
                    sleep_for = min(wait, MAX_BACKOFF_SECONDS) + random.uniform(0, JITTER_SECONDS)
                    logger.info("pixiv 限流：等待 %.1fs 后重试", sleep_for)
                    time.sleep(sleep_for)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue
                if status in (401, 403):
                    raise AppError(
                        "UPSTREAM_UNAVAILABLE", details={"reason": "login_state_invalid"}
                    )
                if 300 <= status < 400:
                    # 未跟随重定向：pixiv 会把失效登录态跳到登录页
                    raise AppError(
                        "UPSTREAM_UNAVAILABLE", details={"reason": "login_state_invalid"}
                    )
                if status == 404:
                    raise not_found({"source": "pixiv"})
                if status >= 500:
                    last_reason = f"http_{status}"
                elif status >= 400:
                    raise AppError(
                        "UPSTREAM_UNAVAILABLE", details={"reason": f"http_{status}"}
                    )
                else:
                    return response

            if attempt < attempts - 1:
                sleep_for = backoff + random.uniform(0, JITTER_SECONDS)
                time.sleep(sleep_for)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": last_reason})
