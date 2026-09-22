"""B 站载荷 → 本服务 ContentItem 的映射。

设计要点（对应 docs/EXECUTION_PLAN.md 13.x）：

- **同构**：产出的字典与 ``app.services.catalog.build_video`` 同形，封面链接一律指回
  本服务的 ``/v1/images/{id}/file``，客户端不知道上游是 ``i*.hdslb.com``。
- **id 前缀**：内容 ID 统一加 ``bv_`` 前缀（如 ``bv_BV1Sve26eENf``），图片代理据此
  路由到 hdslb 回源，同时与本地夹具 ID（``video_0001``）天然隔离。
- **质量优先**：沿用 ``High_quality_Bilibili`` 爬虫的核心指标
  「点赞率 = 点赞数 / 播放数」，只有越过阈值的条目才进入主序列。
- **分区表**：``RANKING_CATEGORIES`` 是 ranking/v2 的分区 slug → ``rid`` 映射，
  与爬虫的 20 个分类对齐。
- **敏感标记**：B 站热门/排行榜是站方已过审的公开榜，没有 R18 之类的机器可读标记，
  因此 ``is_sensitive`` 恒为 False；这里不做没有依据的猜测性过滤。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

from app.core.timeutil import SHANGHAI, now

CONTENT_ID_PREFIX = "bv_"

VIDEO_URL = "https://www.bilibili.com/video/{bvid}"

# 封面缩放后缀：实测 @480w_270h_1c.webp 约 12KB，原图约 230KB
THUMB_SUFFIX = "@480w_270h_1c.webp"
REGULAR_SUFFIX = "@960w_540h_1c.webp"

# 「热门」角标门槛：明显高于质量阈值，避免角标失去区分度
DEFAULT_HOT_LIKE_RATE = 0.15

# 分类排行榜（ranking/v2）分区表：slug → (rid, 中文名)
RANKING_CATEGORIES: dict[str, tuple[int, str]] = {
    "all": (0, "全站"),
    "douga": (1, "动画"),
    "music": (3, "音乐"),
    "game": (4, "游戏"),
    "ent": (5, "娱乐"),
    "knowledge": (36, "知识"),
    "kichiku": (119, "鬼畜"),
    "dance": (129, "舞蹈"),
    "fashion": (155, "时尚"),
    "life": (160, "生活"),
    "guochuang": (168, "国创相关"),
    "documentary": (177, "纪录片"),
    "cinephile": (181, "影视"),
    "tech": (188, "科技"),
    "food": (211, "美食"),
    "animal": (217, "动物圈"),
    "car": (223, "汽车"),
    "sports": (234, "运动"),
}

_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")


# ---------------------------------------------------------------------- ID


def content_id_for(bvid: Any) -> str:
    return f"{CONTENT_ID_PREFIX}{_text(bvid)}"


def bvid_of(content_id: str) -> str | None:
    """``bv_BV1Sve26eENf`` → ``BV1Sve26eENf``；不是 B 站条目时返回 None。"""
    if not content_id.startswith(CONTENT_ID_PREFIX):
        return None
    bvid = content_id[len(CONTENT_ID_PREFIX) :].strip()
    return bvid if _BVID_RE.fullmatch(bvid) else None


def is_bilibili_id(content_id: str) -> bool:
    return bvid_of(content_id) is not None


def category_name(slug: str | None) -> str:
    entry = RANKING_CATEGORIES.get((slug or "").strip().lower())
    return entry[1] if entry else ""


def category_rid(slug: str | None) -> int | None:
    entry = RANKING_CATEGORIES.get((slug or "").strip().lower())
    return entry[0] if entry else None


# ------------------------------------------------------------------ 小工具


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def data_of(envelope: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """取 ``{code, message, ttl, data}`` 的 data；形状不对时返回空字典。"""
    if not isinstance(envelope, Mapping):
        return {}
    return _mapping(envelope.get("data"))


def video_items(envelope: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """热门 / 排行榜信封 → 条目列表（丢掉没有 bvid 的占位项）。"""
    raw = data_of(envelope).get("list")
    if not isinstance(raw, list):
        return []
    return [
        dict(entry)
        for entry in raw
        if isinstance(entry, Mapping) and bvid_of(content_id_for(entry.get("bvid"))) is not None
    ]


def like_rate_of(entry: Mapping[str, Any]) -> float:
    """点赞率 = 点赞数 / 播放数（爬虫的质量指标，播放数为 0 时记 0）。"""
    stat = _mapping(entry.get("stat"))
    view = _int(stat.get("view"))
    if view <= 0:
        return 0.0
    return _int(stat.get("like")) / view


def published_at(entry: Mapping[str, Any]) -> datetime:
    """``pubdate`` 是秒级时间戳；缺失时回落到当前时间。"""
    stamp = _int(entry.get("pubdate"))
    if stamp <= 0:
        return now()
    return datetime.fromtimestamp(stamp, tz=SHANGHAI).replace(microsecond=0)


# -------------------------------------------------------------------- 封面


def normalise_cover(url: Any) -> str:
    """上游 ``pic`` → 规范化的 https 原图地址（去掉可能存在的缩放后缀）。"""
    text = _text(url)
    if not text:
        return ""
    text = text.split("@", 1)[0]
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith("http://"):
        return "https://" + text[len("http://") :]
    return text


def cover_variant(url: Any, variant: str) -> str:
    """按契约的 variant 取封面地址：thumb / regular 走 webp 缩放，original 取原图。"""
    base = normalise_cover(url)
    if not base:
        return ""
    if variant == "thumb":
        return f"{base}{THUMB_SUFFIX}"
    if variant == "regular":
        return f"{base}{REGULAR_SUFFIX}"
    return base


def cover_url_of(entry: Mapping[str, Any]) -> str:
    return normalise_cover(entry.get("pic"))


# ------------------------------------------------------------------ 映射


def build_video(
    entry: Mapping[str, Any],
    *,
    base_url: str,
    category: str | None = None,
    hot_like_rate: float = DEFAULT_HOT_LIKE_RATE,
) -> dict[str, Any]:
    """把一条 B 站条目映射成契约 ContentItem（形状与本地夹具一致）。"""
    from app.services.catalog import file_url  # 局部导入，避免模块级循环依赖

    bvid = _text(entry.get("bvid"))
    content_id = content_id_for(bvid)
    owner = _mapping(entry.get("owner"))
    stat = _mapping(entry.get("stat"))
    play_count = _int(stat.get("view"))
    like_count = _int(stat.get("like"))
    rate = round(like_rate_of(entry), 4)
    uploader = _text(owner.get("name")) or "bilibili"
    # 分区名优先用条目自带的 tname（热门接口给了），否则回落到请求的分区
    name = _text(entry.get("tname")) or (category or "")
    return {
        "id": content_id,
        "type": "video",
        "title": _text(entry.get("title")) or "无标题",
        "subtitle": uploader,
        "cover_url": file_url(base_url, content_id, "thumb"),
        "tags": [name] if name else [],
        "source": "bilibili",
        "source_url": VIDEO_URL.format(bvid=bvid),
        "payload": {
            "bvid": bvid,
            "uploader": uploader,
            "play_count": play_count,
            "published_at": published_at(entry),
            # hot_score 沿用契约的 0-100 量纲：这里就是点赞率百分比
            "hot_score": round(rate * 100, 2),
            "is_hot": rate >= hot_like_rate,
            "duration": _int(entry.get("duration")) or None,
            # 以下为 B 站源独有字段：payload 是自由字典，客户端忽略未知键
            "like_count": like_count,
            "like_rate": rate,
            "category": name,
        },
        "is_sensitive": False,
    }