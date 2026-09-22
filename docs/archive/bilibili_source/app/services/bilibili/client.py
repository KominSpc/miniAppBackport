"""B 站上游 HTTP 客户端（公开接口，不需要登录态）。

对应 ``docs/EXECUTION_PLAN.md`` 13.3 的实测结论：

1. **公开接口**：``x/web-interface/popular``（热门）与 ``x/web-interface/view``
   （单条视频）带头部即可稳定返回 ``code=0``，不携带 Cookie 也能取到完整字段；
   ``x/web-interface/ranking/v2``（分类排行榜）更严格：密集请求后会持续返回 ``-352``
   （风控，实测约 10 分钟后自动恢复），因此调用侧必须限速 + 缓存，见 ``source.py``。
2. **只发 GET、固定身份**：不做 Cookie 池、不轮换账号；命中 429 读 ``Retry-After``，
   缺省则指数退避 + 随机抖动。
3. **错误统一映射**：``-352`` / ``-412`` → ``503 UPSTREAM_UNAVAILABLE``
   （reason ``risk_control``），``-400`` / ``-404`` → ``404 NOT_FOUND``，其余非 0 码
   也按上游不可用处理，绝不把上游 ``message`` 原样透给客户端。
4. **图片回源**：``i*.hdslb.com`` 显式带站点 Referer（实测不带也能取到，带上可避免
   上游策略收紧后失效），并且只允许回源到配置里的图片域名。

``client`` 参数用于测试注入 ``httpx.Client(transport=...)``，不产生真实网络请求。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from app.config import Settings
from app.core.errors import AppError, not_found, rate_limited

logger = logging.getLogger("miniappbackport.bilibili")

ACCEPT_JSON = "application/json, text/plain, */*"

# 退避上限与抖动：避免被判定为攻击流量
MAX_BACKOFF_SECONDS = 30.0
JITTER_SECONDS = 0.8

# 上游业务码：0 成功，-352 触发风控，-412 请求被拦截，-400/-404 参数或资源不存在
CODE_OK = 0
CODE_RISK = frozenset({-352, -412})
CODE_NOT_FOUND = frozenset({-400, -404})


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


class BilibiliClient:
    """最小可用的 B 站客户端：GET JSON 与 GET 字节流。"""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    # ------------------------------------------------------------------ 配置

    @property
    def configured(self) -> bool:
        """热门与排行榜都是公开接口：没有登录态也算配置完成。"""
        return True

    def headers(self, *, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self._settings.bilibili_user_agent,
            "Accept": ACCEPT_JSON,
            "Accept-Language": self._settings.bilibili_accept_language,
            "Referer": referer or self._settings.bilibili_referer,
        }
        cookie = self._settings.bilibili_cookie.strip()
        if cookie:
            # 可选逃生口：风控收紧时填一份浏览器 Cookie（buvid3 等）。
            # 与 pixiv 一致，Cookie 只存在于服务端，绝不下发给客户端。
            headers["Cookie"] = cookie
        return headers

    def transport(self) -> httpx.Client:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "timeout": self._settings.bilibili_timeout_seconds,
                "follow_redirects": False,
            }
            proxy = self._settings.bilibili_proxy.strip()
            if proxy:
                kwargs["proxy"] = proxy
            self._client = httpx.Client(**kwargs)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------ 请求

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """取 ``/x/...`` 接口；信封为 ``{code, message, ttl, data}``。"""
        response = self._request(path, params=params)
        try:
            payload = response.json()
        except ValueError as error:  # 上游返回 HTML（多半是被拦截或改版）
            raise AppError(
                "UPSTREAM_UNAVAILABLE", details={"reason": "invalid_json"}
            ) from error
        if not isinstance(payload, dict):
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "invalid_envelope"})
        code = payload.get("code")
        if code == CODE_OK:
            return payload
        if code in CODE_RISK:
            # 风控：不改写文案，只给一个稳定 reason，便于 /health 与排障
            raise AppError(
                "UPSTREAM_UNAVAILABLE", details={"reason": "risk_control", "code": code}
            )
        if code in CODE_NOT_FOUND:
            raise not_found({"source": "bilibili"})
        raise AppError(
            "UPSTREAM_UNAVAILABLE", details={"reason": "upstream_error", "code": code}
        )

    def get_bytes(self, url: str) -> tuple[bytes, str]:
        """取封面字节。只允许回源到配置的图片域名，避免变成任意地址的代理。"""
        if not self._image_allowed(url):
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "unexpected_image_host"})
        response = self._request(url, referer=self._settings.bilibili_referer)
        content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return response.content, content_type or "image/jpeg"

    def _image_allowed(self, url: str) -> bool:
        hosts = self._settings.bilibili_image_hosts
        if not hosts:
            return True
        return url.startswith(tuple(hosts))

    # ------------------------------------------------------------------ 内部

    def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
    ) -> httpx.Response:
        client = self.transport()
        # 每次请求都显式带上请求头：注入的 httpx.Client（测试）因此不需要预先配置
        headers = self.headers(referer=referer)
        if not url.startswith(("http://", "https://")):
            url = f"{self._settings.bilibili_base_url}{url}"
        attempts = max(1, self._settings.bilibili_max_retries + 1)
        backoff = max(0.1, self._settings.bilibili_retry_initial_seconds)
        last_reason = "unknown"

        for attempt in range(attempts):
            try:
                response = client.request("GET", url, params=params, headers=headers)
            except httpx.HTTPError as error:
                last_reason = type(error).__name__
                logger.warning("bilibili 请求失败（第 %s 次）：%s", attempt + 1, last_reason)
            else:
                status = response.status_code
                if status == 429:
                    wait = _retry_after(response) or backoff
                    if attempt == attempts - 1:
                        raise rate_limited(int(max(1.0, wait)))
                    sleep_for = min(wait, MAX_BACKOFF_SECONDS) + random.uniform(0, JITTER_SECONDS)
                    logger.info("bilibili 限流：等待 %.1fs 后重试", sleep_for)
                    time.sleep(sleep_for)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue
                if status in (401, 403, 412):
                    raise AppError(
                        "UPSTREAM_UNAVAILABLE", details={"reason": "risk_control", "status": status}
                    )
                if 300 <= status < 400:
                    raise AppError(
                        "UPSTREAM_UNAVAILABLE", details={"reason": "unexpected_redirect"}
                    )
                if status == 404:
                    raise not_found({"source": "bilibili"})
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