"""每日推荐：按用户的收藏歌单让 LLM 挑歌，再回源补全成可播放的曲目。

链路：

1. 汇总该用户「收藏夹 + 各个歌单」里的曲名（最多 ``MAX_SEEDS`` 条）作为口味样本；
2. 交给 LLM（OpenAI 兼容协议，复用 ``pet_llm`` 的那个薄客户端）要 N 个歌名；
3. 逐个走 ``music_source.search_tracks`` 回填成真实曲目，搜不到的跳过；
4. 任何一步不成立（没收藏、没配 key、超时、上游报错、一首都没搜到）都回落到平台
   热歌榜 —— 推荐列表因此永远有内容，页面上不会出现空白。

结果按「用户 + 日期」缓存在进程里：一天只问一次 LLM，反复下拉刷新也不会打爆上游。
回源（把歌名搜成曲目）有总时限，模型给不满 30 首时用热歌榜补齐，所以首屏不会
因为上游慢而一直转圈。
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date
from typing import Any, Callable

from app.constants import music as constants
from app.services.music import source as music_source
from app.services.pet_llm import service as llm_service

logger = logging.getLogger("miniappbackport.music")

# user_id -> (日期, 曲目列表)。进程级缓存：单机内测够用，重启即失效。
_CACHE: dict[str, tuple[date, list[dict[str, Any]]]] = {}
_CACHE_MAX = 500

_LIST_PREFIX = re.compile(r"^[\s\-*\d.、)]+")


def _titles_of(tracks: Any) -> list[str]:
    names: list[str] = []
    for track in tracks or []:
        if not isinstance(track, dict):
            continue
        title = str(track.get("title") or "").strip()
        if title:
            names.append(title)
    return names


def seeds_for(user_id: str) -> list[str]:
    """口味样本：收藏夹 + 全部歌单里的曲名（去重，保序）。"""
    seen: set[str] = set()
    seeds: list[str] = []
    groups: list[list[str]] = [_titles_of(music_source.favorite_tracks("", user_id))]
    for playlist in music_source.list_playlists(user_id):
        groups.append(_titles_of(music_source.playlist_tracks(user_id, str(playlist["id"]))))
    for group in groups:
        for title in group:
            if title not in seen:
                seen.add(title)
                seeds.append(title)
            if len(seeds) >= constants.MAX_SEEDS:
                return seeds
    return seeds


def parse_titles(raw: str) -> list[str]:
    """从模型回复里抠歌名：先认 JSON 数组，不行再逐行去序号。"""
    text = (raw or "").strip()
    if not text:
        return []
    match = re.search(r"\[.*\]", text, re.S)
    if match:
        try:
            decoded = json.loads(match.group(0))
        except ValueError:
            decoded = None
        if isinstance(decoded, list):
            return [str(item).strip() for item in decoded if str(item).strip()]
    titles: list[str] = []
    for line in text.splitlines():
        item = _LIST_PREFIX.sub("", line).strip().strip('",')
        if item:
            titles.append(item)
    return titles


def _ask(seeds: list[str], limit: int) -> list[str]:
    raw = llm_service.client().chat(
        [
            {"role": "system", "content": constants.SYSTEM_PROMPT},
            {"role": "user", "content": constants.build_prompt(seeds, limit)},
        ],
        temperature=constants.TEMPERATURE,
        max_tokens=constants.MAX_TOKENS,
    )
    return parse_titles(raw)[: constants.MAX_CANDIDATES]


def _ask_bounded(seeds: list[str], limit: int) -> list[str]:
    """给 LLM 那一轮套一个硬性时限。

    共享的 LLM 客户端自带重试 + 退避，上游卡住时能拖上几分钟；每日推荐是首页数据，
    不能陪着一起等 —— 到点就走回落。线程丢下不管即可（纯 HTTP 调用，不碰共享状态）。
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_ask, seeds, limit)
        return future.result(timeout=constants.LLM_TIMEOUT_SECONDS)
    except FutureTimeout:
        logger.warning("LLM 推荐超时（%.0fs），回落热歌榜", constants.LLM_TIMEOUT_SECONDS)
        raise
    finally:
        pool.shutdown(wait=False)


def _lookup(
    base_url: str,
    titles: list[str],
    *,
    favorited: frozenset[str],
    limit: int,
    deadline: float,
) -> list[dict[str, Any]]:
    """把歌名回源成曲目；搜不到的跳过，凑够 limit 首或到点就停。"""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for title in titles[: constants.MAX_LOOKUPS]:
        if len(items) >= limit or time.monotonic() > deadline:
            break
        try:
            found = music_source.search_tracks(base_url, title, limit=1, favorited_ids=favorited)
        except Exception as exc:  # noqa: BLE001 - 单首搜不到不该毁掉整份推荐
            logger.warning("推荐回源失败 title=%s err=%s", title, exc)
            continue
        for track in found:
            track_id = str(track.get("id") or "")
            if track_id and track_id not in seen:
                seen.add(track_id)
                items.append(track)
                break
    return items


def recommend(
    base_url: str,
    user_id: str,
    *,
    platform: str | None = None,
    limit: int = 30,
    today: date | None = None,
    ask: Callable[[list[str], int], list[str]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """返回 ``(曲目列表, 生效引擎)``；``engine`` 取 ``llm`` / ``cache`` / ``daily``。"""
    day = today or date.today()
    cached = _CACHE.get(user_id)
    if cached is not None and cached[0] == day and len(cached[1]) >= min(limit, 1):
        return cached[1][:limit], "cache"

    favorited = music_source.favorited_ids(user_id)
    items: list[dict[str, Any]] = []
    engine = "daily"
    if llm_service.enabled():
        seeds = seeds_for(user_id)
        if len(seeds) >= constants.MIN_SEEDS:
            try:
                titles = (ask or _ask_bounded)(seeds, limit)
                items = _lookup(
                    base_url,
                    titles,
                    favorited=favorited,
                    limit=limit,
                    deadline=time.monotonic() + constants.LOOKUP_BUDGET_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - LLM 不可用就回落
                logger.warning("LLM 推荐失败，回落热歌榜：%s", exc)
                items = []
            if items:
                engine = "llm"
                # 模型给不满时用热歌榜补齐：列表长度稳定，首屏不会突然变短。
                if len(items) < limit:
                    items = _pad(base_url, items, platform=platform, limit=limit, favorited=favorited)
    if not items:
        items = music_source.top_tracks(
            base_url, platform=platform, limit=limit, favorited_ids=favorited
        )
    _CACHE[user_id] = (day, items)
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    return items[:limit], engine


def _pad(
    base_url: str,
    items: list[dict[str, Any]],
    *,
    platform: str | None,
    limit: int,
    favorited: frozenset[str],
) -> list[dict[str, Any]]:
    """推荐不够 limit 首时，用平台热歌榜按顺序补上（跳过重复）。"""
    seen = {str(item.get("id")) for item in items}
    padded = list(items)
    for track in music_source.top_tracks(
        base_url, platform=platform, limit=limit, favorited_ids=favorited
    ):
        if len(padded) >= limit:
            break
        track_id = str(track.get("id"))
        if track_id and track_id not in seen:
            seen.add(track_id)
            padded.append(track)
    return padded


def reset() -> None:
    """测试 / ``/v1/dev/reset`` 用：清掉当日缓存。"""
    _CACHE.clear()
