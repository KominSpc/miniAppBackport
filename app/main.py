"""应用装配：中间件、异常处理、路由与 CORS。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import openapi as openapi_meta
from app.api.v1 import daily as daily_routes
from app.api.v1 import dev as dev_routes
from app.api.v1 import games as games_routes
from app.api.v1 import images as images_routes
from app.api.v1 import pet as pet_routes
from app.api.v1 import system as system_routes
from app.api.v1 import users as users_routes
from app.config import load_settings, mock_flags_for
from app.core import envelope
from app.core.context import get_request_id, new_request_id, set_request_id
from app.core.errors import AppError, rate_limited
from app.core.mock_middleware import EmptyResultMiddleware
from app.core.ratelimit import limiter
from app.fixtures.images import ensure_sample_images

logger = logging.getLogger("miniappbackport")

# Flutter web 调试时 dev server 端口随机，因此用正则放行本机任意端口
CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
HEALTH_PATH = "/health"


@asynccontextmanager
async def lifespan(app: FastAPI):
    written = ensure_sample_images()
    if written:
        logger.info("已生成本地示例图片 %s 个", written)
    yield


def _error_response(error: AppError) -> JSONResponse:
    response = JSONResponse(
        status_code=error.status,
        content=envelope.fail(error.code, error.message, error.details),
    )
    for key, value in error.headers.items():
        response.headers[key] = value
    return response


def _client_key(request: Request) -> str:
    authorization = request.headers.get("authorization")
    if authorization:
        return authorization
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(
        title=openapi_meta.TITLE,
        version=openapi_meta.VERSION,
        description=openapi_meta.DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # 最内层：只在 MOCK_EMPTY 打开时缓冲并改写列表响应
    app.add_middleware(EmptyResultMiddleware, settings=settings)

    @app.middleware("http")
    async def mock_and_trace(request: Request, call_next):
        set_request_id(request.headers.get("x-request-id") or new_request_id())
        flags = mock_flags_for(request, settings)

        # 健康检查始终可达：它就是用来在异常开关打开时确认服务状态的
        if request.url.path != HEALTH_PATH:
            if flags.rate_limited:
                return _error_response(rate_limited(5))
            retry_after = limiter.retry_after(_client_key(request), settings.rate_limit_per_minute)
            if retry_after:
                return _error_response(rate_limited(retry_after))
            if flags.error:
                return _error_response(AppError("INTERNAL_ERROR"))

        if flags.latency_ms:
            await asyncio.sleep(flags.latency_ms / 1000.0)

        response = await call_next(request)
        response.headers["X-Request-Id"] = get_request_id()
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = {
            "fields": [
                {
                    "loc": ".".join(str(part) for part in error.get("loc", ())),
                    "msg": error.get("msg", ""),
                    "type": error.get("type", ""),
                }
                for error in exc.errors()
            ]
        }
        return _error_response(AppError("VALIDATION_FAILED", details=details))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            code = "NOT_FOUND"
        elif exc.status_code in (400, 405, 406, 415):
            code = "BAD_REQUEST"
        elif exc.status_code == 403:
            code = "FORBIDDEN"
        elif exc.status_code >= 500:
            code = "INTERNAL_ERROR"
        else:
            code = "BAD_REQUEST"
        return _error_response(AppError(code, details={"status": exc.status_code}))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return _error_response(AppError("INTERNAL_ERROR"))

    app.include_router(system_routes.router)
    app.include_router(users_routes.router)
    app.include_router(images_routes.router)
    app.include_router(daily_routes.router)
    app.include_router(games_routes.router)
    app.include_router(pet_routes.router)
    app.include_router(dev_routes.router)

    openapi_meta.install(app)

    # 最后注册，确保 CORS 位于最外层，错误响应与图片字节都带上跨域头
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_extra_origins),
        allow_origin_regex=CORS_ORIGIN_REGEX,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After", "X-Request-Id"],
        max_age=600,
    )
    return app


app = create_app()

