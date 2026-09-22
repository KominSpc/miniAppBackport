"""漫画数据源：进程级配置 + 取数 + 缓存。

`COMIC_SOURCE=node`（默认）时走本机的漫画服务（`tools/comic_service.py`，
上游是 ComicDown 的爬虫库）；没有 mock 数据集 —— 漫画的可用性完全取决于站点，
编一份假数据只会让人误判「漫画能看」。

**搜索策略（用户明确要求）**：按稳定性依次尝试 `manhuagui → qq → dm5`，**取第一个
有结果的站点**，不做多站点混合。理由：三家站点的条目 ID 体系彼此独立，混在一起
翻页会乱；而且上游分页是按站点算的，第 2 页必须接着同一站点翻（客户端会把
首次命中的 `site` 回传，见 `/v1/comics/search` 的 `site` 参数）。

缓存：站点列表、标签、最近更新、详情、章节目录都进进程内 TTL 缓存。**章节目录
缓存时间单独收紧**：manhuagui 的内页地址带签名（`?e=&m=`）会过期，缓存太久会把
过期地址发给客户端（表现为「打开章节全是 403」）。

**站点熔断（必须）**：实测 manhuagui 被墙时，每个请求都会硬挂到超时（HTTP 层没有
RST，就是干等）。而搜索要按顺序试站点，一次搜索就会被一个挂掉的站点拖住十几秒，
客户端 20 秒的读超时直接报「网络超时」。所以这里记「站点 → 恢复时刻」：某站点失败
后冷却期内**直接跳过**，冷却结束再试一次（成功即恢复）。全部站点都在冷却时退回
完整顺序 —— 宁可慢，也不能一个都不试。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from app.config import Settings
from app.core.bytecache import ByteCache
from app.core.errors import AppError
from app.core.timeutil import SHANGHAI
from app.services.comic import adapter
from app.services.comic.client import ComicClient

logger = logging.getLogger("miniappbackport.comic")

# 搜索的站点顺序 = 稳定性顺序（实测：漫画柜最稳、腾讯次之、动漫屋部分条目只有系列页）
SEARCH_ORDER: tuple[str, ...] = ("manhuagui", "qq", "dm5")

# 「搜不到就回落成热门列表」的站点：它们的搜索页在无命中时给的是一堆**无关的热门**，
# 直接当结果用会让人以为搜到了（实测：乱码关键词在 qq 上返回 28 条腾讯热门）。
# 这些站点必须用「标题里得有关键词」兜一道，否则「没找到」永远不会出现。
HOT_FALLBACK_SITES: frozenset[str] = frozenset({"qq", "dm5"})

# 只有这些原因才说明「站点连不上」，值得熔断。上游 404/500 多半是「这个关键词它没有」
# 或者单次抽风（dm5 对无关关键词就返回 500），按 180 秒把站点摘掉反而会误伤。
REACHABILITY_REASONS: frozenset[str] = frozenset(
    {
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ConnectError",
        "ReadError",
        "RemoteProtocolError",
        "ProtocolError",
    }
)

# 关键词归一化时丢掉的字符：空白与常见中英标点（`ONE PIECE` 要能匹配 `onepiece`）
_KEYWORD_NOISE = str.maketrans("", "", " \t\u3000-_·.,:;!?/()[]{}<>「」『』【】《》、，。：；！？…")

# 章节目录的缓存时长：短，因为内页地址带签名会过期
CHAPTER_CACHE_SECONDS = 60.0

# 空结果（搜索无命中 / 某站点最近更新为空）的缓存时长。
#
# 为什么不能按正常 TTL 缓存：站点抽风时会给一页「空」的搜索结果，若按 10 分钟缓存，
# 站点早就恢复了，我们还在拿那份空快照 —— 用户看到的就是「刚才还能搜到，现在搜
# 不到了」，而且会持续好几分钟。这里压到 60 秒：既挡住连续重复请求，又能很快自愈。
EMPTY_RESULT_CACHE_SECONDS = 60.0

# 站点熔断时长（配置缺省时兜底）
DEFAULT_SITE_COOLDOWN_SECONDS = 180.0

_settings: Settings | None = None
_client: ComicClient | None = None
_cache: dict[str, tuple[float, Any]] = {}
_last_error: str | None = None

# 图片字节缓存（封面 / 内页共用）。
#
# 为什么必须有：一屏 20 张封面、一章几十张内页，滚动时同一张图会被反复请求；
# 实测单张内页回源最多 12 秒、封面 0.4–4.2 秒，上游一抖就是用户嘴里的「有时
# 加载不出来」。缓存之后重复请求 0 毫秒，而且并发重复只回源一次（单飞），
# 顺带把触发上游限流的概率压下去。
_IMAGE_CACHE = ByteCache(max_bytes=64 * 1024 * 1024, max_entries=1024)

# 站点 → 恢复时刻（monotonic）。冷却期内该站点不参与取数。
_site_down: dict[str, float] = {}

# 站点 → 连续失败次数。用来把冷却时长做成递进的，见 [_mark_down]。
_site_strikes: dict[str, int] = {}

# 熔断的起步冷却时长：第一次失败只歇这么久，连续失败才按 2 倍递增到
# [DEFAULT_SITE_COOLDOWN_SECONDS] 封顶。爬虫单次抽风（冷启动 6 秒以上）不该
# 让站点消失三分钟——那正是「漫画搜索时好时坏」的来源。
_BREAKER_BASE_COOLDOWN_SECONDS = 30.0

# 并发取数的线程池。
#
# 「最近更新」要把所有存活站点的结果合并，串行跑等于把各站延迟相加：实测腾讯/动漫屋
# 一次 0.2-0.7 秒，而 manhuagui 连不上时要挂满超时，串行一次要 9 秒以上，客户端 20 秒
# 读超时（web 上实际 10 秒就断）直接报「漫画加载失败」。并发之后这一步只等最慢的那个。
_pool_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None

# 线程池最大工作线程数：站点数上限（三个）+ 余量，供详情/章节并发复用
_POOL_MAX_WORKERS = 8


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_POOL_MAX_WORKERS, thread_name_prefix="comic")
        return _pool


def _fetch_all(
    candidates: tuple[str, ...],
    *,
    path_for: Callable[[str], str],
    cache_key_for: Callable[[str], str],
    params_for: Callable[[str], dict[str, Any] | None] | None = None,
    ttl: float | None = None,
    is_empty: Callable[[dict[str, Any]], bool] | None = None,
) -> list[tuple[str, dict[str, Any] | AppError]]:
    """并发请求多个站点，返回 (站点, payload 或错误)，顺序与 [candidates] 一致。

    只用于「本来就要问所有站点」的接口（例如最近更新的合并）。像搜索这种「第一个有
    结果就停」的场景仍旧串行：站点挂掉时并发扇出等于白打两次无用请求，而顺序尝试
    最多慢一个超时，却不会打扰已经失败过的站点。
    """

    def one(site: str) -> tuple[str, dict[str, Any] | AppError]:
        try:
            payload = _get_json(
                path_for(site),
                params=params_for(site) if params_for is not None else None,
                cache_key=cache_key_for(site),
                ttl=ttl,
                is_empty=is_empty,
            )
        except AppError as error:
            return site, error
        return site, payload

    if len(candidates) == 1:
        return [one(candidates[0])]
    return list(_executor().map(one, candidates))


# ------------------------------------------------------------------ 装配


def configure(settings: Settings, *, client: ComicClient | None = None) -> None:
    """启动时注入配置（``create_app`` 调用）；``client`` 供测试注入假传输。"""
    global _settings, _client, _last_error
    # 换客户端时先把上一个的连接池关掉：图片池是常驻的，不关就会随每次
    # configure（启动 + 每个测试用例）泄漏一组 socket。
    previous = _client
    if previous is not None and previous is not client:
        previous.close()
    _settings = settings
    _client = client or (ComicClient(settings) if settings.comic_enabled else None)
    _last_error = None
    _site_down.clear()
    if settings.comic_enabled:
        logger.info(
            "漫画源已启用（上游 %s，图床代理 %s）",
            settings.comic_api_base,
            settings.comic_proxy or "无（直连）",
        )


def active_for(settings: Settings) -> bool:
    return settings.comic_enabled


def active() -> bool:
    return _settings is not None and _settings.comic_enabled


def reset() -> None:
    """清缓存（测试与 /v1/dev/reset 用）。"""
    _cache.clear()
    _IMAGE_CACHE.clear()
    if _client is not None:
        _client.drop_image_transport()
    _site_down.clear()
    _site_strikes.clear()


def last_error() -> str | None:
    return _last_error


def sites() -> tuple[str, ...]:
    configured = _settings.comic_sites if _settings is not None else SEARCH_ORDER
    return tuple(site for site in configured if site) or SEARCH_ORDER


# ------------------------------------------------------------------ 关键词判定


def _normalize_keyword(text: str) -> str:
    return text.translate(_KEYWORD_NOISE).lower()


def keyword_matches(item: dict[str, Any], keyword: str) -> bool:
    """标题里是否有关键词（忽略空白与标点、忽略大小写）。

    只用来识别「搜索页回落成热门榜」——命中率不追求 100%，宁可直接放行的场景
    一律不放（见 [HOT_FALLBACK_SITES]）。
    """
    needle = _normalize_keyword(keyword)
    if not needle:
        return True
    return needle in _normalize_keyword(str(item.get("title") or ""))


# ------------------------------------------------------------------ 站点熔断


def _cooldown_seconds() -> float:
    if _settings is None:
        return DEFAULT_SITE_COOLDOWN_SECONDS
    return float(getattr(_settings, "comic_site_cooldown_seconds", DEFAULT_SITE_COOLDOWN_SECONDS))


def is_down(site: str) -> bool:
    """站点是否处于冷却期。冷却到期自动恢复，这里顺手清掉过期记录。"""
    until = _site_down.get(site)
    if until is None:
        return False
    if time.monotonic() >= until:
        _site_down.pop(site, None)
        return False
    return True


def down_sites() -> tuple[str, ...]:
    """当前处于冷却期的站点（/health 与测试用）。"""
    return tuple(site for site in sites() if is_down(site))


def _should_open_breaker(error: AppError) -> bool:
    """这次失败是否说明「站点连不上」。只有这种才熔断，见 REACHABILITY_REASONS。"""
    if error.code != "UPSTREAM_UNAVAILABLE":
        return False
    reason = str(error.details.get("reason") or "")
    return reason in REACHABILITY_REASONS or reason.startswith("invalid_")


def _mark_down(site: str, error: AppError) -> None:
    """记一次失败：够得上熔断条件才开闸（`_last_error` 由 `_get_json` 统一记）。

    冷却时长随连续失败次数翻倍递增：第一次 30 秒，第二次 60 秒……封顶在配置的
    冷却时长（默认 180 秒）。这样单次抽风只损失半分钟，真挂了的站点又能很快停手。
    """
    if not _should_open_breaker(error):
        return
    strikes = _site_strikes.get(site, 0) + 1
    _site_strikes[site] = strikes
    cooldown = min(_BREAKER_BASE_COOLDOWN_SECONDS * (2 ** (strikes - 1)), _cooldown_seconds())
    _site_down[site] = time.monotonic() + cooldown
    logger.warning(
        "站点 %s 连不上，冷却 %.0f 秒内跳过（连续第 %d 次）：%s",
        site,
        cooldown,
        strikes,
        error.details,
    )


def _mark_up(site: str) -> None:
    _site_strikes.pop(site, None)
    if _site_down.pop(site, None) is not None:
        logger.info("站点 %s 已恢复", site)


def _ordered_sites() -> tuple[str, ...]:
    """可用站点，保持配置顺序；全部在冷却期内时退回完整顺序。"""
    all_sites = sites()
    alive = tuple(site for site in all_sites if not is_down(site))
    return alive or all_sites


# ------------------------------------------------------------------ 查询


def search(
    query: str, *, page: int = 1, site: str | None = None, base_url: str = ""
) -> tuple[list[dict[str, Any]], str | None]:
    """关键词搜索，返回 (条目, 命中的站点)。

    不指定 [site] 时按稳定性顺序依次尝试，**第一个有结果的站点胜出**；翻页时客户端
    要带上这个站点，否则第二页会从别的站点取（ID 与分页都对不上）。
    """
    keyword = query.strip()
    if not keyword:
        return [], None

    candidates = (site,) if site else _ordered_sites()
    for candidate in candidates:
        try:
            payload = _get_json(
                f"/api/{candidate}/search",
                params={"name": keyword, "page": page},
                cache_key=f"search:{candidate}:{keyword}:{page}",
            )
        except AppError as error:
            # 显式指定站点也不放过：熔断只影响「我们自己的候选顺序」，
            # 客户端带 site 的翻页请求照旧直连该站点，只是它会被记上一笔。
            _mark_down(candidate, error)
            logger.warning("站点 %s 搜索失败，换下一个：%s", candidate, error.details)
            continue
        _mark_up(candidate)
        items = [
            adapter.summary(raw, site=candidate, base_url=base_url)
            for raw in payload.get("search_result") or []
        ]
        if candidate in HOT_FALLBACK_SITES:
            relevant = [item for item in items if keyword_matches(item, keyword)]
            if items and not relevant:
                logger.info(
                    "站点 %s 对「%s」返回 %d 条却都不含关键词（搜索页回落成热门），换下一个",
                    candidate,
                    keyword,
                    len(items),
                )
            items = relevant
        if items:
            return items, candidate
    return [], site


def latest(*, page: int = 1, site: str | None = None, base_url: str = "") -> list[dict[str, Any]]:
    """最近更新。多站点合并去重（热门/推荐都用它当池子）。"""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    candidates = (site,) if site else _ordered_sites()
    for candidate, payload in _fetch_all(
        candidates,
        path_for=lambda target: f"/api/{target}/latest",
        params_for=lambda target: {"page": page},
        cache_key_for=lambda target: f"latest:{target}:{page}",
        is_empty=lambda data: not data.get("latest"),
    ):
        if isinstance(payload, AppError):
            _mark_down(candidate, payload)
            logger.warning("站点 %s 最近更新失败：%s", candidate, payload.details)
            continue
        _mark_up(candidate)
        for raw in payload.get("latest") or []:
            item = adapter.summary(raw, site=candidate, base_url=base_url)
            if not item["comic_id"] or item["id"] in seen:
                continue
            seen.add(item["id"])
            merged.append(item)
    return merged


def tags(*, site: str | None = None) -> list[dict[str, Any]]:
    """标签分组。默认按稳定性顺序取**第一个有标签的站点**（漫画柜分类最全）。

    漫画柜挂掉时也要能出标签，否则整条标签栏会空着（客户端把它当成「没有分类」）。
    """
    for target in (site,) if site else _ordered_sites():
        try:
            payload = _get_json(f"/api/{target}/tags", cache_key=f"tags:{target}")
        except AppError as error:
            _mark_down(target, error)
            logger.warning("站点 %s 标签失败，换下一个：%s", target, error.details)
            continue
        _mark_up(target)
        groups = adapter.tag_groups(payload.get("tags"))
        if groups:
            return groups
    return []


def tag_list(
    tag: str, *, page: int = 1, site: str | None = None, base_url: str = ""
) -> list[dict[str, Any]]:
    target = site or (_ordered_sites() or SEARCH_ORDER)[0]
    payload = _get_json(
        f"/api/{target}/list",
        params={"tag": tag, "page": page},
        cache_key=f"tag:{target}:{tag}:{page}",
    )
    _mark_up(target)
    return [
        adapter.summary(raw, site=target, base_url=base_url)
        for raw in payload.get("list") or []
    ]


def detail(site: str, raw_comic_id: str, *, base_url: str = "") -> dict[str, Any]:
    payload = _get_json(
        f"/api/{site}/comic/{raw_comic_id}",
        cache_key=f"comic:{site}:{raw_comic_id}",
    )
    return adapter.detail(payload, site=site, base_url=base_url)


def chapter(
    site: str, raw_comic_id: str, number: int, *, ext_name: str = "", base_url: str = ""
) -> dict[str, Any]:
    payload = _get_json(
        f"/api/{site}/comic/{raw_comic_id}/{number}",
        params={"ext_name": ext_name} if ext_name else None,
        cache_key=f"chapter:{site}:{raw_comic_id}:{ext_name}:{number}",
        ttl=CHAPTER_CACHE_SECONDS,
    )
    return adapter.chapter_content(
        payload,
        site=site,
        raw_comic_id=raw_comic_id,
        number=number,
        ext_name=ext_name,
        base_url=base_url,
    )


def random_page(
    *, page: int = 1, limit: int = 20, seed: str | None = None, base_url: str = ""
) -> tuple[list[dict[str, Any]], bool]:
    """随机推荐：把「最近更新」合并成池子，按 [seed] 稳定洗牌后切片。

    同一个 seed + page 的结果是稳定的（便于分页不重样），**换一个 seed 就是一套
    新顺序**：客户端滑到底时换 seed 接着要，就能一直刷下去。
    """
    pool = _random_pool(base_url)
    if not pool:
        return [], False
    rng = random.Random(seed or _default_seed())
    ordered = list(pool)
    rng.shuffle(ordered)
    start = max(0, (page - 1) * limit)
    items = ordered[start : start + limit]
    return items, start + len(items) < len(ordered)


def image_bytes(url: str) -> tuple[bytes, str]:
    """取一张漫画图片的字节（带该站点的防盗链策略 + 进程内缓存）。"""
    return _IMAGE_CACHE.load(url, lambda: _fetch_image(url))


def _fetch_image(url: str) -> tuple[bytes, str]:
    """真正回源取图；只在缓存未命中时被调用一次。

    封面走的是「原图档 + 缩略档回落」：爬虫给的 `/cpic/b/` 换成了 `/cpic/`（见
    adapter.upgrade_cover），但少数带 `_98` / `_85` 后缀的封面没有原图档，图床直接
    503。这里抓到失败就退到 `h/`（180×240），列表里仍比原来的 132×176 清楚。
    """
    try:
        return _read_image(url)
    except AppError as error:
        fallback = adapter.cover_fallback(url)
        if fallback is None:
            raise
        logger.warning("封面原图不可用，回落到缩略档：%s（%s）", fallback, error.details)
        return _read_image(fallback)


def _read_image(url: str) -> tuple[bytes, str]:
    """回源一次:取字节与 content-type。"""
    client = _require_client()
    lease, response = client.open_image(url)
    try:
        body = response.read()
        content_type = response.headers.get("content-type") or "image/jpeg"
    finally:
        lease.close()
    if not body:
        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "empty_image"})
    return body, content_type.split(";")[0].strip()


# ------------------------------------------------------------------ 内部


def _default_seed() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d")


def _random_pool(base_url: str = "") -> list[dict[str, Any]]:
    """随机池：三个站点的「最近更新」合并去重（每站一页，实测 ~260 条）。

    各站的 latest 各自进缓存，因此这个池子第一次拉完就几乎不花时间；TTL 到期后
    自动换一批新条目，随机推荐跟着变。
    """
    # 三个站点各自的 latest 有自己的缓存，合并逻辑与 [latest] 完全一样，
    # 所以直接复用（并行 + 去重都在里面做了）。
    return latest(page=1, base_url=base_url)


def _require_client() -> ComicClient:
    client = _client
    if client is None:
        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "comic_service_not_configured"})
    return client


def _get_json(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    cache_key: str,
    ttl: float | None = None,
    is_empty: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    global _last_error
    lifetime = _ttl() if ttl is None else ttl
    cached = _fresh(cache_key)
    if cached is not None:
        return cached
    client = _require_client()
    try:
        payload = client.get_json(path, params=params)
    except AppError as error:
        _last_error = str(error.details.get("reason") or error.code)
        raise
    # 「空结果」只短暂缓存：站点抽风时给的空页不能压住整个 TTL，
    # 否则站点恢复了我们还在用旧快照，用户看到的是搜索一直没结果。
    if is_empty is not None and is_empty(payload):
        lifetime = min(lifetime, EMPTY_RESULT_CACHE_SECONDS)
    _cache[cache_key] = (time.monotonic() + lifetime, payload)
    return payload


def _ttl() -> float:
    return float(_settings.comic_cache_ttl_seconds if _settings else 600)


def _fresh(key: str) -> Any | None:
    """缓存项存的是「到期时刻」，因此不同 TTL 的条目可以共用一个字典。"""
    found = _cache.get(key)
    if found is None:
        return None
    expires_at, value = found
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        return None
    return value
