"""B 站数据源：进程级配置 + 取数 + 回源缓存。

``VIDEO_SOURCE=bilibili`` 时由本模块接管 ``catalog.all_videos`` / ``item_by_id`` /
``/v1/images/{id}/file`` 的取数；默认仍是 mock，行为完全不变。

上游实测结论（2026-09-19，见 docs/EXECUTION_PLAN.md 13.3）：

- ``x/web-interface/popular``（热门）：``ps=20&pn=N`` 翻页，稳定返回 ``code=0``；
- ``x/web-interface/ranking/v2``（分类榜）：密集请求后会持续返回 ``-352``（风控，实测约
  10 分钟后自动恢复）。因此分类榜比热门榜脆弱，这里做三层保护：**限速 + 串行 + TTL 缓存**，
  并在风控命中时回落到上一次的陈旧缓存（stale-while-error）。

缓存策略：

- 热门页：按 ``pn`` 缓存 ``BILIBILI_CACHE_TTL_SECONDS``（默认 600s）；
- 分类榜：按 ``rid`` 缓存同样的 TTL（同一分区刷新不会重复打上游）；
- 封面链接：按内容 ID 缓存 ``BILIBILI_COVER_TTL_SECONDS``（默认 3600s），列表取数时
  顺带写入，图片代理因此零额外请求；缓存失效时用 ``view`` 接口自愈。

进程级状态是刻意的：配置在 ``create_app`` 时注入一次，之后只读；测试用
``configure`` / ``reset`` 显式控制。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.config import Settings
from app.core.errors import AppError
from app.services.bilibili import adapter
from app.services.bilibili.client import BilibiliClient

logger = logging.getLogger("miniappbackport.bilibili")

# 热门接口每页固定 20 条；最多翻 5 页，够筛出首屏要的高质量视频
POPULAR_PAGE_SIZE = 20
MAX_POPULAR_PAGES = 5
# 最少翻的页数：质量阈值会滤掉约 2/3 条目，多取一页让「高质量优先」更稳
MIN_POPULAR_PAGES = 2

_settings: Settings | None = None
_client: BilibiliClient | None = None
_popular_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
_ranking_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
_cover_cache: dict[str, tuple[float, str]] = {}
_item_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# 最近一次上游失败原因（/health 用）：None 表示最近一次调用成功。
_last_error: str | None = None

# 串行 + 最小间隔：单进程内不并发打上游，避免自造风控
_lock = threading.Lock()
_last_call_at = 0.0


# ------------------------------------------------------------------ 装配


def configure(settings: Settings, *, client: BilibiliClient | None = None) -> None:
    """启动时注入配置（``create_app`` 调用）；``client`` 供测试注入假传输。"""
    global _settings, _client, _last_error
    _settings = settings
    _client = client or (BilibiliClient(settings) if settings.bilibili_enabled else None)
    if settings.bilibili_enabled:
        logger.info("视频内容源已切换为 bilibili（公开接口，无需登录态）")


def reset() -> None:
    """测试用：清空配置与缓存。"""
    global _settings, _client, _last_error, _last_call_at
    if _client is not None:
        _client.close()
    _settings = None
    _client = None
    _popular_cache.clear()
    _ranking_cache.clear()
    _cover_cache.clear()
    _item_cache.clear()
    _last_error = None
    _last_call_at = 0.0


def settings() -> Settings | None:
    return _settings


def active() -> bool:
    return _settings is not None and _settings.bilibili_enabled


def client() -> BilibiliClient:
    if _client is None:
        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "bilibili_not_configured"})
    return _client


def degraded() -> bool:
    """真实视频源启用且最近一次上游调用失败。

    与 pixiv 一致：不做主动探测（``/health`` 必须便宜），上游状态由最近一次真实请求记录。
    """
    if not active():
        return False
    return _last_error is not None


def last_error() -> str | None:
    return _last_error


# ------------------------------------------------------------------ 缓存


def _ttl() -> float:
    return float(_settings.bilibili_cache_ttl_seconds) if _settings else 600.0


def _cover_ttl() -> float:
    return float(_settings.bilibili_cover_ttl_seconds) if _settings else 3600.0


def _fresh(entry: tuple[float, Any] | None, ttl: float) -> Any | None:
    if entry is None:
        return None
    stored_at, value = entry
    if ttl > 0 and time.monotonic() - stored_at > ttl:
        return None
    return value


def _stale(entry: tuple[float, Any] | None) -> Any | None:
    """过期但仍有价值的结果：风控命中时的兜底。"""
    return entry[1] if entry is not None else None


def remember_cover(content_id: str, url: str) -> None:
    """记录封面地址（列表取数与 ``view`` 自愈时调用）。"""
    if content_id and url:
        _cover_cache[content_id] = (time.monotonic(), url)


# ------------------------------------------------------------------ 调用


def _throttle() -> None:
    """最小请求间隔。实测 B 站对密集请求敏感，这里主动降速。"""
    global _last_call_at
    interval = float(_settings.bilibili_min_interval_seconds) if _settings else 0.0
    if interval > 0:
        wait = _last_call_at + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    _last_call_at = time.monotonic()


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """串行执行上游调用，并统一记录上游健康状态。"""
    global _last_error
    with _lock:
        _throttle()
        try:
            value = fn(*args, **kwargs)
        except AppError as error:
            _last_error = str(error.details.get("reason") or error.code)
            raise
        except Exception as error:  # noqa: BLE001 —— 只记录，不改变对外语义
            _last_error = type(error).__name__
            raise
        _last_error = None
        return value


# ------------------------------------------------------------------ 取数


def all_videos(
    base_url: str,
    *,
    favorited_ids: frozenset[str] = frozenset(),
    safe_mode: bool = True,
    category: str | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """热门综合榜 / 分类排行榜 → ContentItem 列表（质量优先）。

    ``safe_mode`` 目前不影响视频列表：B 站公开榜没有机器可读的敏感标记
    （见 ``adapter`` 的说明），参数保留是为了与图片源同形。
    """
    wanted = max(1, limit)
    slug = (category or "").strip().lower() or None
    name = adapter.category_name(slug)
    result: list[dict[str, Any]] = []
    for entry in _quality_pool(slug, wanted)[:wanted]:
        item = adapter.build_video(entry, base_url=base_url, category=name)
        cover = adapter.cover_url_of(entry)
        if cover:
            remember_cover(item["id"], cover)
        if item["id"] in favorited_ids:
            item["payload"]["is_favorited"] = True
        result.append(item)
    return result


def item_by_id(
    content_id: str,
    *,
    base_url: str,
    favorited: bool = False,
    safe_mode: bool = True,
) -> dict[str, Any] | None:
    """``bv_<bvid>`` → 详情（``x/web-interface/view``，结果进 TTL 缓存）。"""
    bvid = adapter.bvid_of(content_id)
    if bvid is None:
        return None
    entry = _fresh(_item_cache.get(content_id), _ttl())
    if entry is None:
        envelope = _call(client().get_json, "/x/web-interface/view", params={"bvid": bvid})
        data = adapter.data_of(envelope)
        if not data:
            return None
        entry = dict(data)
        _item_cache[content_id] = (time.monotonic(), entry)
    item = adapter.build_video(entry, base_url=base_url)
    cover = adapter.cover_url_of(entry)
    if cover:
        remember_cover(content_id, cover)
    if favorited:
        item["payload"]["is_favorited"] = True
    return item


def image_bytes(content_id: str, variant: str, page: int) -> tuple[bytes, str] | None:
    """封面回源（thumb / regular / original）。越界或无法定位封面返回 None。"""
    bvid = adapter.bvid_of(content_id)
    if bvid is None:
        return None
    url = _cover_for(content_id, bvid)
    if not url:
        return None
    target = adapter.cover_variant(url, variant)
    if not target:
        return None
    return _call(client().get_bytes, target)


# ------------------------------------------------------------------ 内部


def _cover_for(content_id: str, bvid: str) -> str:
    """封面地址：命中缓存直接返回；未命中用 ``view`` 自愈；风控时回落陈旧缓存。"""
    cached = _fresh(_cover_cache.get(content_id), _cover_ttl())
    if cached:
        return cached
    try:
        envelope = _call(client().get_json, "/x/web-interface/view", params={"bvid": bvid})
    except AppError:
        return _stale(_cover_cache.get(content_id)) or ""
    cover = adapter.cover_url_of(adapter.data_of(envelope))
    if cover:
        remember_cover(content_id, cover)
        return cover
    return _stale(_cover_cache.get(content_id)) or ""


def _quality_pool(slug: str | None, limit: int) -> list[dict[str, Any]]:
    """质量优先的条目池：越过阈值的在前，不够时按点赞率补齐。"""
    threshold = _settings.bilibili_like_rate_threshold if _settings else 0.1
    entries = _ranking_entries(slug) if slug else _popular_entries(limit)
    ordered = sorted(entries, key=adapter.like_rate_of, reverse=True)
    passing = [entry for entry in ordered if adapter.like_rate_of(entry) > threshold]
    if len(passing) >= limit:
        return passing
    rest = [entry for entry in ordered if adapter.like_rate_of(entry) <= threshold]
    return passing + rest[: limit - len(passing)]


def _popular_entries(limit: int) -> list[dict[str, Any]]:
    pages = min(MAX_POPULAR_PAGES, max(MIN_POPULAR_PAGES, -(-limit // POPULAR_PAGE_SIZE) + 1))
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        for entry in _popular_page(page):
            bvid = str(entry.get("bvid") or "")
            if bvid and bvid not in seen:
                seen.add(bvid)
                entries.append(entry)
    return entries


def _popular_page(page: int) -> list[dict[str, Any]]:
    cached = _fresh(_popular_cache.get(page), _ttl())
    if cached is not None:
        return cached
    envelope = _call(
        client().get_json,
        "/x/web-interface/popular",
        params={"ps": POPULAR_PAGE_SIZE, "pn": page},
    )
    entries = adapter.video_items(envelope)
    _popular_cache[page] = (time.monotonic(), entries)
    return entries


def _ranking_entries(slug: str) -> list[dict[str, Any]]:
    rid = adapter.category_rid(slug)
    if rid is None:
        return []
    cached = _fresh(_ranking_cache.get(rid), _ttl())
    if cached is not None:
        return cached
    try:
        envelope = _call(
            client().get_json,
            "/x/web-interface/ranking/v2",
            params={"rid": rid, "type": "all"},
        )
    except AppError:
        stale = _stale(_ranking_cache.get(rid))
        if stale is None:
            raise
        logger.info("bilibili 分类榜取数失败，回落到缓存结果 rid=%s", rid)
        return stale
    entries = adapter.video_items(envelope)
    _ranking_cache[rid] = (time.monotonic(), entries)
    return entries