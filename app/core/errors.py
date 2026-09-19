"""错误码定义与统一异常类型。

错误码表与 contract/openapi.json 的 ErrorCode 枚举逐项对应。
"""

from __future__ import annotations

from typing import Any

# code -> (HTTP 状态码, 可直接展示的中文文案)
ERROR_TABLE: dict[str, tuple[int, str]] = {
    "BAD_REQUEST": (400, "请求参数不合法，请重置后重试"),
    "UNAUTHORIZED": (401, "登录状态缺失或无效，请重新初始化"),
    "TOKEN_EXPIRED": (401, "登录状态已过期，请重新初始化"),
    "FORBIDDEN": (403, "当前环境不允许该操作"),
    "NOT_FOUND": (404, "内容不存在或已下架"),
    "CONFLICT": (409, "操作冲突，请刷新后重试"),
    "VALIDATION_FAILED": (422, "参数校验失败，请检查输入"),
    "RATE_LIMITED": (429, "请求过于频繁，请稍后再试"),
    "INTERNAL_ERROR": (500, "服务暂时不可用，请稍后再试"),
    "UPSTREAM_UNAVAILABLE": (503, "内容源暂时不可用，请稍后再试"),
}

ERROR_CODES: tuple[str, ...] = tuple(ERROR_TABLE)


def status_for(code: str) -> int:
    return ERROR_TABLE.get(code, ERROR_TABLE["INTERNAL_ERROR"])[0]


def message_for(code: str) -> str:
    return ERROR_TABLE.get(code, ERROR_TABLE["INTERNAL_ERROR"])[1]


class AppError(Exception):
    """业务异常，由全局处理器包装成统一信封。"""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if code not in ERROR_TABLE:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message or message_for(code))
        self.code = code
        self.status = status_for(code)
        self.message = message or message_for(code)
        self.details = details or {}
        self.headers = headers or {}


def bad_request(details: dict[str, Any] | None = None) -> AppError:
    return AppError("BAD_REQUEST", details=details)


def not_found(details: dict[str, Any] | None = None) -> AppError:
    return AppError("NOT_FOUND", details=details)


def unauthorized(code: str = "UNAUTHORIZED") -> AppError:
    return AppError(code)


def forbidden(details: dict[str, Any] | None = None) -> AppError:
    return AppError("FORBIDDEN", details=details)


def rate_limited(retry_after: int) -> AppError:
    return AppError(
        "RATE_LIMITED",
        details={"retry_after": retry_after},
        headers={"Retry-After": str(retry_after)},
    )
