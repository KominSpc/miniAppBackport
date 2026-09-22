"""冻结契约里「手写」的那部分内容，改由服务端生成。

Flutter 仓库的 ``contract/openapi.json`` 是本服务 OpenAPI 的导出结果
（``scripts/export_openapi.py --sync`` 会整份覆盖）。因此契约快照里需要长期存在的
手写内容 —— schema 示例、错误码与 HTTP 状态的对应说明 —— 必须由服务端产出，
否则每次同步都会把它们抹掉（客户端契约测试会因此失败）。

这里只生成与契约测试相关的三块：

1. ``components.schemas.ContentItem.examples``：四种内容类型各一个示例；
2. ``components.schemas.ErrorCode.description``：错误码与 HTTP 状态映射；
3. ``components.schemas.ErrorResponse.examples``：限流错误信封示例。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ERROR_TABLE

BASE_URL = "http://127.0.0.1:18421"


def _file_url(content_id: str, variant: str) -> str:
    return f"{BASE_URL}/v1/images/{content_id}/file?variant={variant}"


def _source_url(content_id: str) -> str:
    return f"{BASE_URL}/mock/source/{content_id}"


# 示例取值与夹具（app/fixtures/data/*.json）保持一致，便于对照排查。
IMAGE_EXAMPLE: dict[str, Any] = {
    "id": "img_0001",
    "type": "image",
    "title": "樱花下的少女",
    "subtitle": "澄空Aoi",
    "cover_url": _file_url("img_0001", "thumb"),
    "tags": ["初音ミク", "VOCALOID", "桜"],
    "source": "mock",
    "source_url": _source_url("img_0001"),
    "payload": {
        "author": "澄空Aoi",
        "thumbnail_url": _file_url("img_0001", "thumb"),
        "image_url": _file_url("img_0001", "original"),
        "width": 1400,
        "height": 975,
        "aspect_ratio": 1.4359,
        "is_favorited": False,
        "illust_type": 0,
        "created_at": "2026-09-19T08:00:00+08:00",
    },
    "is_sensitive": False,
}

VIDEO_EXAMPLE: dict[str, Any] = {
    "id": "video_0001",
    "type": "video",
    "title": "【4K】二次元神曲合集",
    "subtitle": "星之声频道",
    "cover_url": _file_url("video_0001", "thumb"),
    "tags": ["音乐", "合集"],
    "source": "mock",
    "source_url": _source_url("video_0001"),
    "payload": {
        "bvid": "BV16v917U2Pr",
        "uploader": "星之声频道",
        "play_count": 1234567,
        "published_at": "2026-09-19T03:30:00+08:00",
        "hot_score": 98.5,
        "is_hot": True,
        "duration": 512,
    },
    "is_sensitive": False,
}

GAME_EXAMPLE: dict[str, Any] = {
    "id": "game_0001",
    "type": "game",
    "title": "星轨幻想",
    "subtitle": "Android · iOS",
    "cover_url": _file_url("game_0001", "thumb"),
    "tags": ["角色扮演", "二次元"],
    "source": "mock",
    "source_url": _source_url("game_0001"),
    "payload": {
        "platforms": ["Android", "iOS"],
        "genres": ["角色扮演", "二次元"],
        "updated_at": "2026-09-19T07:20:00+08:00",
        "description": "回合制卡牌 RPG，主打剧情与角色养成。",
        "version": "2.3.0",
        "developer": "星辉工作室",
        "rating": 8.7,
        "today_updated": True,
    },
    "is_sensitive": False,
}

CARD_EXAMPLE: dict[str, Any] = {
    "id": "card_0001",
    "type": "card",
    "title": "看看「初音ミク」的其他作品",
    "subtitle": "宠物对话推荐",
    "cover_url": _file_url("img_0001", "thumb"),
    "tags": ["推荐"],
    "source": "mock",
    "source_url": _source_url("card_0001"),
    "payload": {"target_type": "image", "target_id": "img_0001"},
    "is_sensitive": False,
}


def content_item_examples() -> list[dict[str, Any]]:
    """四种内容类型的示例，供客户端做往返解析测试。"""
    return [IMAGE_EXAMPLE, VIDEO_EXAMPLE, GAME_EXAMPLE, CARD_EXAMPLE]


def error_code_description() -> str:
    """错误码与 HTTP 状态的对应说明，由 ERROR_TABLE 派生，不会与实现漂移。"""
    pairs = "、".join(
        f"{code}={status}" for code, (status, _) in ERROR_TABLE.items()
    )
    return (
        "统一错误码及其 HTTP 状态映射（客户端按此表做重试与重新注册判断）："
        f"{pairs}。"
    )


def error_examples() -> list[dict[str, Any]]:
    """错误信封示例：以最需要客户端处理的 429 限流为例。"""
    code = "RATE_LIMITED"
    retry_after = 5
    return [
        {
            "data": None,
            "meta": {
                "request_id": "req_01J8W3F5K6M7N8P9Q0R1S2T3V4",
                "next_cursor": None,
                "has_more": False,
                "source": "mock",
                "version": "mock-2026.09",
            },
            "error": {
                "code": code,
                "message": ERROR_TABLE[code][1],
                "details": {"retry_after": retry_after},
            },
        }
    ]