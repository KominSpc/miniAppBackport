"""OpenAI 兼容的 chat/completions 客户端。

刻意做得极薄：只负责发请求、把上游异常翻译成统一错误码，不做任何提示词与
质检逻辑（那些在 ``persona`` 与 ``service`` 里）。``client`` 参数用于注入
``httpx.MockTransport``，测试因此不产生真实网络请求。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from app.config import Settings
from app.constants import llm as constants
from app.core.errors import AppError

logger = logging.getLogger("miniappbackport.pet_llm")

MAX_BACKOFF_SECONDS = 8.0
JITTER_SECONDS = 0.3


class LlmClient:
    """最小可用的 OpenAI 兼容客户端。"""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    # ------------------------------------------------------------------ 配置

    @property
    def configured(self) -> bool:
        """没配 base_url 或 key 时视为未启用，接口回落到规则引擎。"""
        return bool(self._settings.llm_base_url.strip() and self._settings.llm_api_key.strip())

    @property
    def model(self) -> str:
        return self._settings.llm_model.strip() or constants.DEFAULT_MODEL

    @property
    def provider(self) -> str:
        return self._settings.llm_provider.strip() or "openai-compatible"

    def transport(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._settings.llm_timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------ 对话

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """发一轮对话，返回助手那句话的原文（未做任何质检）。"""
        if not self.configured:
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "llm_not_configured"})
        url = f"{self._settings.llm_base_url.rstrip('/')}{constants.CHAT_COMPLETIONS_PATH}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": (
                constants.DEFAULT_TEMPERATURE if temperature is None else temperature
            ),
            "max_tokens": constants.DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens,
            "stream": False,
        }
        response = self._request(url, payload)
        return self._extract(response)

    # ------------------------------------------------------------------ 内部

    def _extract(self, response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError as error:
            raise AppError(
                "UPSTREAM_UNAVAILABLE", details={"reason": "llm_invalid_json"}
            ) from error
        if not isinstance(body, dict):
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "llm_invalid_envelope"})
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "llm_empty_choices"})
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "llm_empty_completion"})
        return content

    def _request(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        client = self.transport()
        headers = {
            "Authorization": f"{constants.AUTHORIZATION_SCHEME} {self._settings.llm_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        attempts = max(1, self._settings.llm_max_retries + 1)
        backoff = max(0.1, self._settings.llm_retry_initial_seconds)
        last_reason = "unknown"

        for attempt in range(attempts):
            try:
                response = client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as error:
                last_reason = type(error).__name__
                logger.warning("LLM 请求失败（第 %s 次）：%s", attempt + 1, last_reason)
            else:
                status = response.status_code
                if status == 429:
                    if attempt == attempts - 1:
                        raise AppError(
                            "UPSTREAM_UNAVAILABLE", details={"reason": "llm_rate_limited"}
                        )
                    wait = float(response.headers.get("retry-after") or backoff)
                    time.sleep(min(max(wait, 0.5), MAX_BACKOFF_SECONDS))
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue
                if status in (401, 403):
                    raise AppError(
                        "UPSTREAM_UNAVAILABLE", details={"reason": "llm_unauthorized"}
                    )
                if status == 404:
                    raise AppError(
                        "UPSTREAM_UNAVAILABLE", details={"reason": "llm_model_not_found"}
                    )
                if status >= 400:
                    # 400 通常是提示词或参数被上游拒绝，重试没有意义
                    if status < 500:
                        raise AppError(
                            "UPSTREAM_UNAVAILABLE",
                            details={"reason": f"llm_http_{status}"},
                        )
                    last_reason = f"llm_http_{status}"
                else:
                    return response

            if attempt < attempts - 1:
                time.sleep(backoff + random.uniform(0, JITTER_SECONDS))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": last_reason})
