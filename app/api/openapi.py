"""导出的 OpenAPI 文档与契约快照对齐所需的元信息与后处理。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel

from app.schemas.common import ErrorResponse
from app.schemas.content import CardPayload, GamePayload, ImagePayload, VideoPayload

TITLE = "miniAppBackport 模拟服务契约"
VERSION = "1.0.0-mock.1"
DESCRIPTION = (
    "二次元美图与每日内容应用的客户端-服务端契约（W1 冻结版）。"
    "所有客户端接口统一 /v1 前缀，响应统一为 {data, meta, error} 信封。"
    "首期由 FastAPI 模拟服务实现；后续替换真实适配器时路径与结构不变。"
)

CONTRACT_NOTES = [
    "meta.version 为内容数据版本，模拟期为 mock-2026.09。",
    "所有时间戳为 ISO 8601 且带偏移，时区固定 Asia/Shanghai (+08:00)。",
    "cover_url / thumbnail_url / image_url 必须指向本服务同源地址，禁止返回第三方原始链接。",
]

SERVERS = [
    {"url": "http://127.0.0.1:8000", "description": "本机开发"},
    {
        "url": "http://{host}:8000",
        "description": "局域网真机联调",
        "variables": {"host": {"default": "192.168.1.10"}},
    },
]

TAGS = [
    {"name": "system", "description": "健康检查与开发工具"},
    {"name": "users", "description": "匿名用户、偏好与历史"},
    {"name": "images", "description": "美图推荐、搜索、收藏与图片代理"},
    {"name": "daily", "description": "每日冷知识与 B 站热点视频"},
    {"name": "games", "description": "今日更新与游戏总单"},
    {"name": "pet", "description": "宠物对话、专家路由与动作建议"},
]

# 契约把 payload 的四种形状单独作为 schema 暴露，方便客户端按 type 生成模型；
# 服务端实际由 ContentItem.payload 承载并按 type 分派，这里把四个 schema 补进文档。
PAYLOAD_MODELS: tuple[type[BaseModel], ...] = (ImagePayload, VideoPayload, GamePayload, CardPayload)

UNUSED_SCHEMAS = ("HTTPValidationError", "ValidationError")


def _as_ref(model: type[BaseModel]) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def _payload_schemas() -> dict[str, Any]:
    collected: dict[str, Any] = {}
    for model in PAYLOAD_MODELS:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schema.pop("title", None)
        for name, sub in (schema.pop("$defs", None) or {}).items():
            collected.setdefault(name, sub)
        collected[model.__name__] = schema
    return collected


def _fix_validation_responses(schema: dict[str, Any]) -> None:
    """FastAPI 默认的 422 文档指向 HTTPValidationError，而实际返回的是统一错误信封。"""
    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if not isinstance(responses, dict):
                continue
            entry = responses.get("422")
            if not isinstance(entry, dict) or not entry.get("content"):
                continue
            if "HTTPValidationError" in json.dumps(entry):
                responses["422"] = {
                    "description": "422 参数校验失败（统一错误信封）",
                    "content": {"application/json": {"schema": _as_ref(ErrorResponse)}},
                }


def _mark_public_operations(schema: dict[str, Any]) -> None:
    """没有安全要求的操作显式写成 security: []，与契约里免鉴权接口的写法一致。"""
    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if isinstance(operation, dict) and "responses" in operation and "security" not in operation:
                operation["security"] = []


def install(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=TITLE,
            version=VERSION,
            description=DESCRIPTION,
            routes=app.routes,
            servers=SERVERS,
            tags=TAGS,
        )
        schema["info"]["x-contract-notes"] = CONTRACT_NOTES

        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        for name, payload in _payload_schemas().items():
            schemas.setdefault(name, payload)

        _fix_validation_responses(schema)
        _mark_public_operations(schema)

        # 统一错误信封取代了 FastAPI 默认的校验错误模型，这两个 schema 不再被引用
        for name in UNUSED_SCHEMAS:
            schemas.pop(name, None)

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

