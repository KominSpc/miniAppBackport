"""Pixiv 数据源：进程级配置 + 取数 + 回源缓存。

``CONTENT_SOURCE=pixiv`` 时由本模块接管 ``catalog.all_images`` / ``item_by_id`` /
``/v1/images/{id}/file`` 的取数；默认仍是 mock，行为完全不变。

缓存策略（对应 7.2 第 2/4 条「避免 N+1」）：

- **榜单**：按 ``mode`` 缓存 ``PIXIV_RANKING_TTL``（默认 300s），一个请求只打一次上游；
- **每页链接**：按 pid 缓存 ``pixiv_cache_ttl_seconds``（默认 3600s）。榜单返回的
  ``url`` 会先塞进 page 0 的缓存，首屏缩略图因此零额外请求；只有原图/多图才回源。
- **图片字节**：不做磁盘缓存（后续接 CDN 或对象存储时再加，见 9.3 技术债）。

进程级状态是刻意的：配置在 ``create_app`` 时注入一次，之后只读；测试用
``configure`` / ``reset`` 显式控制。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from urllib.parse import quote
from typing import Any

from app.config import Settings
from app.core.errors import AppError, not_found
from app.services.pixiv import adapter
from app.services.pixiv.client import PixivClient

logger = logging.getLogger("miniappbackport.pixiv")

RANKING_TTL_SECONDS = 300

# 关键词搜索：逐页缓存（key 为 ``关键词:页码``），展示侧按 offset+limit 决定取到第几页。
SEARCH_TTL_SECONDS = 300
SEARCH_PAGE_SIZE = 60
SEARCH_MAX_PAGES = 10
SEARCH_ORDER = "date_d"
# 上游 ``type`` 参数：作品类型筛选，取值对齐参考实现 pixiv-api.js 的
# illust / manga / ugoira（``all`` 表示不筛）。实测上游会忽略它，因此本地还要按
# ``illustType`` 兜底过滤（见 :func:`search_images`）。
SEARCH_TYPE_ALL = "all"
SEARCH_TYPE_ILLUST = "illust"
SEARCH_TYPE_MANGA = "manga"
SEARCH_TYPE_UGOIRA = "ugoira"
# ``type`` 取值 → 上游 ``illustType`` 数值（0 插画 / 1 漫画 / 2 动图）。
ILLUST_TYPE_BY_KIND = {
    SEARCH_TYPE_ILLUST: 0,
    SEARCH_TYPE_MANGA: 1,
    SEARCH_TYPE_UGOIRA: 2,
}
# 上游 ``illustType``：2 = 动图（ugoira）。
UGOIRA_ILLUST_TYPE = 2
# 上游 lang 参数默认值：影响标签 / 标题的翻译文案
SEARCH_LANG_DEFAULT = "zh"

_settings: Settings | None = None
_client: PixivClient | None = None
_ranking_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_pages_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_search_cache: dict[str, tuple[float, tuple[list[dict[str, Any]], int]]] = {}
# 作者作品 ID 集合：``profile/all`` 只随新投稿变化，缓存久一点没关系
_artist_ids_cache: dict[str, tuple[float, list[str]]] = {}
# 作者头像 / 昵称：同上，投稿之外几乎不变
_artist_profile_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# 动图帧表：按 pid 缓存。命中空 dict 表示「已确认不是动图」，别再打上游。
_ugoira_meta_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# 作者主页：昵称 / 头像 / 作品 ID 的缓存时长
ARTIST_TTL_SECONDS = 600
# 批取作者作品详情时每次带多少个 id（URL 长度与请求数的折中，与客户端一致）
ARTIST_BATCH_SIZE = 48

# 最近一次上游失败原因（/health 用）：None 表示最近一次调用成功。
_last_error: str | None = None



# ------------------------------------------------------------------ 装配


def configure(settings: Settings, *, client: PixivClient | None = None) -> None:
    """启动时注入配置（``create_app`` 调用）；``client`` 供测试注入假传输。"""
    global _settings, _client, _last_error
    _settings = settings
    _client = client or (PixivClient(settings) if settings.pixiv_enabled else None)
    if settings.pixiv_enabled:
        logger.info(
            "内容源已切换为 pixiv（登录态 %s）",
            "已配置" if settings.pixiv_cookie.strip() else "缺失（推荐流字段会减少）",
        )


def reset() -> None:
    """测试用：清空配置与缓存。"""
    global _settings, _client, _last_error
    if _client is not None:
        _client.close()
    _settings = None
    _client = None
    _ranking_cache.clear()
    _pages_cache.clear()
    _search_cache.clear()
    _artist_ids_cache.clear()
    _artist_profile_cache.clear()
    _ugoira_meta_cache.clear()
    _last_error = None


def settings() -> Settings | None:
    return _settings


def active() -> bool:
    return _settings is not None and _settings.pixiv_enabled


def client() -> PixivClient:
    if _client is None:
        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "pixiv_not_configured"})
    return _client


def degraded() -> bool:
    """真实内容源启用但登录态缺失，或最近一次上游调用失败。

    供 ``/health`` 使用：不做主动探测（健康检查必须便宜），上游状态由最近一次
    真实请求记录（见 docs/EXECUTION_PLAN.md 7.4 第 2 条）。
    """
    if _settings is None or not _settings.pixiv_enabled:
        return False
    return not login_state_ok() or _last_error is not None


def last_error() -> str | None:
    """最近一次上游失败原因；成功一次即清空。"""
    return _last_error


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """统一记录上游健康状态，并把失败原样抛出（错误映射在 client 内完成）。"""
    global _last_error
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


def login_state_ok() -> bool:
    """「已配置登录态」≠「登录态有效」；有效性只能由真实请求判定（见 /health）。"""
    return _client is not None and _client.configured


# ------------------------------------------------------------------ 缓存


def _fresh(entry: tuple[float, Any] | None, ttl: float) -> Any | None:
    if entry is None:
        return None
    stored_at, value = entry
    if ttl > 0 and time.monotonic() - stored_at > ttl:
        return None
    return value


def seed_pages(illust_id: Any, urls: list[dict[str, str]]) -> None:
    _pages_cache[str(illust_id)] = (time.monotonic(), urls)


def pages_for(illust_id: str) -> list[dict[str, str]]:
    """每页链接；命中缓存直接返回，否则回源 ``/ajax/illust/{pid}/pages``。"""
    ttl = float(_settings.pixiv_cache_ttl_seconds if _settings else 0)
    cached = _fresh(_pages_cache.get(illust_id), ttl)
    if cached:
        return cached
    envelope = _call(client().get_json, f"/ajax/illust/{illust_id}/pages", params={"lang": "zh"})
    urls = adapter.page_urls(envelope)
    if urls:
        seed_pages(illust_id, urls)
    return urls


# ------------------------------------------------------------------ 取数


def ranking(mode: str = "daily", *, limit: int = 50) -> list[dict[str, Any]]:
    """排行榜条目（真实上游），按 mode 做 TTL 缓存。"""
    key = f"{mode}:{limit}"
    cached = _fresh(_ranking_cache.get(key), float(RANKING_TTL_SECONDS))
    if cached is not None:
        return cached
    envelope = _call(
        client().get_json,

        "/ranking.php", params={"mode": mode, "content": "illust", "format": "json"}
    )
    items = adapter.ranking_illusts(envelope)[: max(1, limit)]
    for entry in items:
        # 榜单自带的 url 是 page 0 的缩略图，先塞进缓存避免首屏 N+1
        thumb = str(entry.get("url") or "").strip()
        pid = str(entry.get("illust_id") or entry.get("id") or "")
        if thumb and pid:
            seed_pages(pid, [{"thumb_mini": thumb, "small": thumb, "regular": thumb}])
    _ranking_cache[key] = (time.monotonic(), items)
    return items


def search_entries(
    keyword: str,
    *,
    pages: int,
    min_bookmarks: int | None = None,
    lang: str | None = None,
    allow_r18: bool = False,
    illust_type: str = SEARCH_TYPE_ALL,
) -> list[dict[str, Any]]:
    """按需取前 ``pages`` 页关键词搜索结果，跨页去重。筛选参数原样透传给上游。"""
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    last_page = max(1, pages)
    for page in range(1, max(1, pages) + 1):
        if page > last_page:
            break
        entries, last_page = _search_page(
            keyword,
            page,
            min_bookmarks=min_bookmarks,
            lang=lang,
            allow_r18=allow_r18,
            illust_type=illust_type,
        )
        if not entries:
            break
        for entry in entries:
            pid = str(entry.get("id") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            # 搜索结果自带方形缩略图：先塞进 page 0 缓存，首屏缩略图零额外请求（同榜单）
            thumb = str(entry.get("url") or "").strip()
            if thumb:
                seed_pages(pid, [{"thumb_mini": thumb, "small": thumb, "regular": thumb}])
            collected.append(entry)
    return collected


def _search_page(
    keyword: str,
    page: int,
    *,
    min_bookmarks: int | None = None,
    lang: str | None = None,
    allow_r18: bool = False,
    illust_type: str = SEARCH_TYPE_ALL,
) -> tuple[list[dict[str, Any]], int]:
    """单页搜索结果（TTL 缓存，缓存键含全部筛选条件）。返回（条目, 上游最后一页）。

    上游参数对应：``bl`` = 收藏数下限、``lang`` = 翻译语言、``mode`` = safe / all、
    ``type`` = all / illust / manga / ugoira（作品类型）。

    注意：**上游会忽略 ``type``**（实测 type=all/illust/manga/ugoira 返回完全一致），
    未登录时连 ``bl`` / ``mode`` 也一并忽略（R18 榜直接 403），所以作品类型与 R18
    都要靠本地兜底：R18 只能等配置了 ``PIXIV_COOKIE`` 才有真实结果，类型按
    ``illustType`` 在 :func:`search_images` 里过滤。
    """
    key = ":".join(
        [
            keyword.strip().lower(),
            str(page),
            str(min_bookmarks or 0),
            lang or "",
            "r18" if allow_r18 else "safe",
            illust_type,
        ]
    )
    cached = _fresh(_search_cache.get(key), float(SEARCH_TTL_SECONDS))
    if cached is not None:
        return cached
    word = keyword.strip()
    params: dict[str, Any] = {
        "word": word,
        "order": SEARCH_ORDER,
        "mode": "all" if allow_r18 else "safe",
        "p": page,
        "s_mode": "s_tag",
        "type": illust_type,
        "lang": lang or SEARCH_LANG_DEFAULT,
    }
    if min_bookmarks:
        params["bl"] = min_bookmarks
    envelope = _call(
        client().get_json,
        f"/ajax/search/artworks/{quote(word, safe='')}",
        params=params,
    )
    entries, last_page = adapter.search_illusts(envelope)
    value = (entries, last_page)
    _search_cache[key] = (time.monotonic(), value)
    return value


def matches_illust_type(entry: dict[str, Any], illust_type: str) -> bool:
    """作品类型过滤：``all`` 放行，其余按上游 ``illustType`` 比对。

    上游忽略 ``type`` 参数（实测 all / illust / manga / ugoira 四种取值返回完全一致），
    所以类型筛选只能在本地（与客户端直连路径的 PixivMapper.matchesKind 对齐）。
    """
    expected = ILLUST_TYPE_BY_KIND.get(illust_type)
    if expected is None:
        return True
    return adapter.illust_type(entry) == expected


def search_images(
    base_url: str,
    keyword: str,
    *,
    favorited_ids: frozenset[str] = frozenset(),
    safe_mode: bool = True,
    offset: int = 0,
    limit: int = 20,
    min_bookmarks: int | None = None,
    lang: str | None = None,
    allow_r18: bool = False,
    illust_type: str = SEARCH_TYPE_ALL,
) -> list[dict[str, Any]]:
    """关键词搜索 → ContentItem 列表（真实上游 ``/ajax/search/artworks``）。

    只取「够当前这一页用」的上游页数：``offset + limit`` 落在第几页就取到第几页，
    逐页做 TTL 缓存（缓存键含筛选条件），因此翻页与重复搜索都不会重复打上游。

    **多取一页**：调用方用「取到的条目数 > 窗口末尾」判断还有没有下一页，窗口恰好
    装满就会被误判成到底（表现为搜索翻两三页就「已经到底啦」），所以每次都多看一页。

    **作品类型要接着翻**：上游忽略 ``type``，只能本地按 ``illustType`` 过滤，
    而混合结果里漫画 / 动图占比很低（实测「初音ミク」60 条里只有 1 条漫画、
    4 条动图），所以筛出的条目不够当前这页时自动接着翻，最多
    ``SEARCH_MAX_PAGES`` 页（逐页 TTL 缓存，重复翻页不会重复打上游）。
    """
    word = keyword.strip()
    if not word:
        return []
    wanted = max(0, offset) + max(1, limit)
    # all 不需要额外翻页：一页上游就是「够用 + 多一页」的量级；其余类型由下面的
    # 填充循环按需继续翻。
    lookahead = 1 if illust_type == SEARCH_TYPE_ALL else 0
    pages = min(SEARCH_MAX_PAGES, max(1, -(-wanted // SEARCH_PAGE_SIZE) + lookahead))
    matching: list[dict[str, Any]] = []
    while True:
        matching = [
            entry
            for entry in search_entries(
                word,
                pages=pages,
                min_bookmarks=min_bookmarks,
                lang=lang,
                allow_r18=allow_r18,
                illust_type=illust_type,
            )
            if matches_illust_type(entry, illust_type)
        ]
        if len(matching) > wanted or pages >= SEARCH_MAX_PAGES:
            break
        pages = min(SEARCH_MAX_PAGES, pages + 1)
    result: list[dict[str, Any]] = []
    for entry in matching:
        item = adapter.build_illust(entry, base_url=base_url)
        if safe_mode and item["is_sensitive"]:
            continue
        if item["id"] in favorited_ids:
            item["payload"]["is_favorited"] = True
        result.append(adapter.strip_internal(item))
    return result


def _artist_ids(user_id: str) -> list[str]:
    """作者全部作品 ID（新作在前）。

    ``profile/all`` 的 ``illusts`` / ``manga`` 都是 ``id → null`` 的映射，键顺序不
    保证，因此显式按 ID 数值倒序排一次：作者页「往下翻」才是稳定的时间线。
    """
    sections = _artist_sections(user_id)
    return [*sections["illusts"], *sections["manga"]]


def _artist_sections(user_id: str) -> dict[str, list[str]]:
    """``profile/all`` 的两段作品 ID（插画 / 漫画），各自按 ID 倒序。"""
    ttl = float(ARTIST_TTL_SECONDS)
    cached = _fresh(_artist_ids_cache.get(user_id), ttl)
    if cached is not None:
        return cached
    envelope = _call(
        client().get_json, f"/ajax/user/{user_id}/profile/all", params={"lang": "zh"}
    )
    body = adapter.body_of(envelope)
    sections: dict[str, list[str]] = {}
    for section in ("illusts", "manga"):
        bucket = body.get(section) if isinstance(body, Mapping) else None
        ids: list[str] = []
        if isinstance(bucket, Mapping):
            ids = [str(key).strip() for key in bucket.keys()]
        # 去重（保序）后按数值倒序；非数字键理论上不会有，真出现时排在后面
        unique = list(dict.fromkeys(value for value in ids if value.isdigit()))
        unique.sort(key=int, reverse=True)
        sections[section] = unique
    _artist_ids_cache[user_id] = (time.monotonic(), sections)
    return sections


def artist_profile(user_id: str) -> dict[str, Any]:
    """作者主页信息（昵称 / 头像 / 作品数）。

    与客户端直连同名功能对应：web 端浏览器直连 pixiv 会被 CORS 拦下，这条后端通道
    是回退路径；安卓端直连失败时同样可以回退到这里。
    """
    uid = user_id.strip()
    if not uid:
        raise not_found({"reason": "artist_id_empty"})
    ttl = float(ARTIST_TTL_SECONDS)
    cached = _fresh(_artist_profile_cache.get(uid), ttl)
    if cached is not None:
        return cached
    envelope = _call(client().get_json, f"/ajax/user/{uid}", params={"lang": "zh"})
    body = adapter.body_of(envelope)
    if not isinstance(body, Mapping):
        raise not_found({"user_id": uid, "reason": "artist_not_found"})
    ids = _artist_ids(uid)
    # profile/all 里插画与漫画分开给，这里按真实条数拆开，界面才能分别展示
    sections = _artist_sections(uid)
    profile = {
        "user_id": uid,
        "name": str(body.get("name") or body.get("userId") or uid).strip() or uid,
        "avatar_url": str(body.get("imageBig") or body.get("image") or "").strip(),
        "illust_count": len(sections["illusts"]),
        "manga_count": len(sections["manga"]),
        "work_count": len(ids),
        "page_url": f"https://www.pixiv.net/users/{uid}",
    }
    _artist_profile_cache[uid] = (time.monotonic(), profile)
    return profile


def artist_works(
    base_url: str,
    user_id: str,
    *,
    offset: int = 0,
    limit: int = 20,
    favorited_ids: frozenset[str] = frozenset(),
    safe_mode: bool = True,
) -> list[dict[str, Any]]:
    """作者作品列表（分页）。

    两步走：``profile/all`` 拿全部作品 ID，再按批用 ``profile/illusts`` 换成详情。
    上游没有「第 N 页」这种接口，所以分页就是 ID 数组上的偏移量。

    刻意**不逐件调** ``/ajax/illust/{pid}``：那条路一件一请求，配合上游的节流，
    一屏 20 件要等一分钟。
    """
    uid = user_id.strip()
    if not uid:
        raise not_found({"reason": "artist_id_empty"})
    all_ids = _artist_ids(uid)
    start = max(0, offset)
    slice_ids = all_ids[start : start + max(1, limit)]
    if not slice_ids:
        return []
    works: dict[str, Mapping[str, Any]] = {}
    for batch_start in range(0, len(slice_ids), ARTIST_BATCH_SIZE):
        batch = slice_ids[batch_start : batch_start + ARTIST_BATCH_SIZE]
        envelope = _call(
            client().get_json,
            f"/ajax/user/{uid}/profile/illusts",
            params={
                "work_category": "illust",
                "is_first_page": 1,
                "lang": "zh",
                # httpx 把 list 值展开成重复的 `ids[]=...`，与网页端发出的形状一致
                "ids[]": batch,
            },
        )
        body = adapter.body_of(envelope)
        bucket = body.get("works") if isinstance(body, Mapping) else None
        if isinstance(bucket, Mapping):
            for key, value in bucket.items():
                if isinstance(value, Mapping):
                    works[str(key)] = value
    result: list[dict[str, Any]] = []
    for pid in slice_ids:
        raw = works.get(pid)
        if raw is None:
            continue
        # profile/illusts 的条目**不带作者字段**（都在这位作者名下），补进去，
        # 否则卡片副标题会退化成「pixiv」。
        entry = dict(raw)
        entry.setdefault("userId", uid)
        entry.setdefault("userName", _artist_name(uid))
        item = adapter.build_illust(entry, base_url=base_url)
        if safe_mode and item["is_sensitive"]:
            continue
        if item["id"] in favorited_ids:
            item["payload"]["is_favorited"] = True
        result.append(adapter.strip_internal(item))
    return result


def _artist_name(user_id: str) -> str:
    """作者昵称；拿不到就空串（卡片会退化成「pixiv」）。"""
    try:
        return str(artist_profile(user_id).get("name") or "")
    except AppError:
        return ""


def all_images(
    base_url: str,
    *,
    favorited_ids: frozenset[str] = frozenset(),
    safe_mode: bool = True,
    mode: str = "daily",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """榜单 → ContentItem 列表。敏感内容由服务端强制过滤（7.4 第 4 条）。"""
    result: list[dict[str, Any]] = []
    for entry in ranking(mode, limit=limit):
        pid = str(entry.get("illust_id") or entry.get("id") or "")
        if not pid:
            continue
        item = adapter.build_illust(entry, base_url=base_url)
        if safe_mode and item["is_sensitive"]:
            continue
        if item["id"] in favorited_ids:
            item["payload"]["is_favorited"] = True
        result.append(adapter.strip_internal(item))
    return result


def item_by_id(
    content_id: str,
    *,
    base_url: str,
    favorited: bool = False,
    safe_mode: bool = True,
) -> dict[str, Any] | None:
    """``px_<pid>`` → 详情。上游 404 由客户端映射成 NOT_FOUND。"""
    pid = adapter.illust_id_of(content_id)
    if pid is None:
        return None
    envelope = _call(client().get_json, f"/ajax/illust/{pid}")
    body = adapter.body_of(envelope)
    if not isinstance(body, dict):
        return None
    item = adapter.build_illust(body, base_url=base_url, favorited=favorited)
    if safe_mode and item["is_sensitive"]:
        return None
    return adapter.strip_internal(item)


def ugoira_meta(illust_id: str) -> dict[str, Any] | None:
    """动图（うごイラ）的帧表；作品不是动图（或已被删除）时返回 None。

    动图**不是视频**：上游返回一个 zip（内含按序排列的 JPEG 帧）加逐帧延时，
    客户端必须自己按 ``frames`` 的顺序与延时播放。所以这里只产出
    ``{src, original_src, frames}``，zip 字节由 :func:`ugoira_archive` 单独取。

    这里刻意**不走** [_call]：探测「这张是不是动图」失败是常规分支（插画就是
    会报错），不该把它记成上游故障影响 /health。
    """
    ttl = float(_settings.pixiv_cache_ttl_seconds if _settings else 0)
    cached = _fresh(_ugoira_meta_cache.get(illust_id), ttl)
    if cached is not None:
        return cached or None
    try:
        envelope = client().get_json(
            f"/ajax/illust/{illust_id}/ugoira_meta", params={"lang": "zh"}
        )
    except AppError as error:
        # 只有「上游明确答复：这张不是动图 / 作品不存在」（实测 HTTP 404，映射成
        # NOT_FOUND）才缓存「没有」，避免每次点开普通插画都回源一次。
        #
        # 超时 / 5xx / 429 / 登录态失效**绝不能写缓存**：缓存形状是空 dict，而空
        # dict 的语义是「这不是动图」，一旦写进去，这张动图在 TTL 内就永远是静态
        # 图了（用户实测到的「第一次网络失败，第二次直接按缓存判失败」）。
        if error.code == "NOT_FOUND":
            _ugoira_meta_cache[illust_id] = (time.monotonic(), {})
        else:
            logger.warning(
                "ugoira_meta 探测失败但不缓存（可重试）：pid=%s reason=%s",
                illust_id,
                error.details.get("reason") or error.code,
            )
        return None
    meta = _ugoira_payload(adapter.body_of(envelope))
    _ugoira_meta_cache[illust_id] = (time.monotonic(), meta or {})
    return meta


def _ugoira_payload(body: Any) -> dict[str, Any] | None:
    """把上游的 ugoira_meta body 收成内部形状；帧表为空视为非动图。"""
    if not isinstance(body, Mapping):
        return None
    frames: list[dict[str, Any]] = []
    raw_frames = body.get("frames")
    for entry in raw_frames if isinstance(raw_frames, list) else []:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("file") or "").strip()
        if not name:
            continue
        try:
            delay = int(entry.get("delay") or 0)
        except (TypeError, ValueError):
            delay = 0
        frames.append({"file": name, "delay_ms": max(0, delay)})
    if not frames:
        return None
    src = str(body.get("src") or "").strip()
    original_src = str(body.get("originalSrc") or "").strip()
    if not src and not original_src:
        return None
    return {"src": src, "original_src": original_src, "frames": frames}


def ugoira_archive(illust_id: str, *, original: bool = False) -> tuple[bytes, str] | None:
    """动图 zip 的字节（``(content, media_type)``）；不是动图时返回 None。"""
    meta = ugoira_meta(illust_id)
    if meta is None:
        return None
    url = str(meta["original_src"] if original else meta["src"]).strip()
    if not url:
        # 只有一种分辨率可用时退到另一种，别让「原图」按钮变成 404
        url = str(meta["src"] or meta["original_src"]).strip()
    if not url:
        return None
    # 只回源到配置的图片域名：链接来自上游 JSON，别让它变成任意地址的代理
    host = _settings.pixiv_image_host if _settings else ""
    if host and not url.startswith(host):
        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "unexpected_image_host"})
    return _call(client().get_bytes, url)


def image_bytes(content_id: str, variant: str, page: int) -> tuple[bytes, str] | None:
    """回源图片字节（thumb / regular / original）。越界或条目不存在返回 None。"""
    pid = adapter.illust_id_of(content_id)
    if pid is None:
        return None
    urls = pages_for(pid)
    if page < 0 or page >= len(urls):
        if not urls:
            raise not_found({"id": content_id, "reason": "no_pages"})
        return None
    url = adapter.pick_url(urls[page], variant)
    if not url:
        return None
    # 列表卡片要的 thumb 统一抬到 540 档：上游各列表给的规格从 128 到 360 不等，
    # 128/250 那两档摆在卡片上就是糊的（作者页最明显）。
    if variant == "thumb":
        url = adapter.upgrade_thumb(url)
    # 只回源到配置的图片域名：链接来自上游 JSON，别让它变成任意地址的代理
    host = _settings.pixiv_image_host if _settings else ""
    if host and not url.startswith(host):
        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "unexpected_image_host"})
    return _call(client().get_bytes, url)
