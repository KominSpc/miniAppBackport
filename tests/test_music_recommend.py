"""每日推荐：有 LLM 就按收藏挑歌，任何一步不成立都回落热歌榜。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.services.music import recommend as rec


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    rec.reset()
    yield
    rec.reset()


def _stub_source(
    monkeypatch: pytest.MonkeyPatch,
    *,
    favorites: list[dict[str, Any]] | None = None,
    playlists: list[dict[str, Any]] | None = None,
    playlist_tracks: dict[str, list[dict[str, Any]]] | None = None,
    found: dict[str, str] | None = None,
    top: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """把上游换成内存桩；返回一个调用计数器。"""
    calls = {"search": 0}
    table = found or {}

    def _search(base_url: str, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls["search"] += 1
        track_id = table.get(query)
        return [{"id": track_id, "title": query}] if track_id else []

    monkeypatch.setattr(rec.music_source, "favorite_tracks", lambda base, uid: list(favorites or []))
    monkeypatch.setattr(rec.music_source, "list_playlists", lambda uid: list(playlists or []))
    monkeypatch.setattr(
        rec.music_source,
        "playlist_tracks",
        lambda uid, pid: list((playlist_tracks or {}).get(pid, [])),
    )
    monkeypatch.setattr(rec.music_source, "favorited_ids", lambda uid: frozenset())
    monkeypatch.setattr(rec.music_source, "search_tracks", _search)
    monkeypatch.setattr(rec.music_source, "top_tracks", lambda base, **kwargs: list(top or []))
    return calls


def _enable_llm(monkeypatch: pytest.MonkeyPatch, enabled: bool = True) -> None:
    monkeypatch.setattr(rec.llm_service, "enabled", lambda: enabled)


def test_seeds_come_from_favorites_and_playlists(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_source(
        monkeypatch,
        favorites=[{"title": "夜曲"}, {"title": "夜曲"}, {"title": "稻香"}],
        playlists=[{"id": "pl_1"}],
        playlist_tracks={"pl_1": [{"title": "晴天"}, {"title": "稻香"}]},
    )
    assert rec.seeds_for("u1") == ["夜曲", "稻香", "晴天"]


def test_llm_titles_are_resolved_into_playable_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_llm(monkeypatch)
    _stub_source(
        monkeypatch,
        favorites=[{"title": "夜曲"}, {"title": "稻香"}, {"title": "晴天"}],
        found={"新歌 A": "ne_1", "新歌 B": "ne_2"},
        top=[{"id": "hot_1"}],
    )

    items, engine = rec.recommend(
        "http://x", "u1", limit=2, ask=lambda seeds, limit: ["新歌 A", "搜不到的歌", "新歌 B"]
    )

    assert engine == "llm"
    assert [item["id"] for item in items] == ["ne_1", "ne_2"]


def test_llm_failure_falls_back_to_top_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_llm(monkeypatch)
    _stub_source(
        monkeypatch,
        favorites=[{"title": "夜曲"}, {"title": "稻香"}, {"title": "晴天"}],
        top=[{"id": "hot_1"}, {"id": "hot_2"}],
    )

    def boom(seeds: list[str], limit: int) -> list[str]:
        raise RuntimeError("llm down")

    items, engine = rec.recommend("http://x", "u1", limit=2, ask=boom)

    assert engine == "daily"
    assert [item["id"] for item in items] == ["hot_1", "hot_2"]


def test_no_favorites_skips_the_llm_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_llm(monkeypatch)
    _stub_source(monkeypatch, top=[{"id": "hot_1"}])
    asked: list[int] = []

    items, engine = rec.recommend(
        "http://x", "u1", limit=1, ask=lambda seeds, limit: asked.append(1) or ["x"]
    )

    assert engine == "daily"
    assert [item["id"] for item in items] == ["hot_1"]
    assert asked == [], "没有收藏就没有口味样本，不该白问一次模型"


def test_llm_disabled_uses_top_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_llm(monkeypatch, enabled=False)
    _stub_source(
        monkeypatch,
        favorites=[{"title": "夜曲"}, {"title": "稻香"}, {"title": "晴天"}],
        top=[{"id": "hot_1"}],
    )

    _, engine = rec.recommend("http://x", "u1", limit=1, ask=lambda seeds, limit: ["新歌 A"])

    assert engine == "daily"


def test_one_result_per_day_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_llm(monkeypatch)
    calls = _stub_source(
        monkeypatch,
        favorites=[{"title": "夜曲"}, {"title": "稻香"}, {"title": "晴天"}],
        found={"新歌 A": "ne_1"},
    )
    asked: list[int] = []

    def ask(seeds: list[str], limit: int) -> list[str]:
        asked.append(1)
        return ["新歌 A"]

    day = date(2026, 9, 22)
    first, engine = rec.recommend("http://x", "u1", limit=1, today=day, ask=ask)
    second, engine2 = rec.recommend("http://x", "u1", limit=1, today=day, ask=ask)

    assert engine == "llm" and engine2 == "cache"
    assert first == second
    assert len(asked) == 1, "同一天只问一次模型"
    assert calls["search"] == 1


def test_parse_titles_accepts_json_and_plain_lines() -> None:
    assert rec.parse_titles('["歌名 - 歌手", "另一首"]') == ["歌名 - 歌手", "另一首"]
    assert rec.parse_titles("1. 歌名 - 歌手\n2、另一首") == ["歌名 - 歌手", "另一首"]
    assert rec.parse_titles("") == []


def test_too_few_favorites_skips_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """收藏只有一两首时没有口味样本，别白问一次模型。"""
    _enable_llm(monkeypatch)
    _stub_source(monkeypatch, favorites=[{"title": "夜曲"}], top=[{"id": "hot_1"}])
    asked: list[int] = []

    _, engine = rec.recommend(
        "http://x", "u1", limit=1, ask=lambda seeds, limit: asked.append(1) or ["新歌 A"]
    )

    assert engine == "daily"
    assert asked == []


def test_short_llm_list_is_padded_from_the_hot_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型只给出一首、其余搜不到时，用热歌榜补齐，列表长度保持稳定。"""
    _enable_llm(monkeypatch)
    _stub_source(
        monkeypatch,
        favorites=[{"title": "a"}, {"title": "b"}, {"title": "c"}],
        found={"新歌 A": "ne_1"},
        top=[{"id": "hot_1"}, {"id": "ne_1"}],
    )

    items, engine = rec.recommend("http://x", "u1", limit=2, ask=lambda seeds, limit: ["新歌 A"])

    assert engine == "llm"
    assert [item["id"] for item in items] == ["ne_1", "hot_1"]


def test_lookup_stops_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游慢的时候不许无限搜下去：到点就用手上的结果 + 热歌榜补齐。"""
    _enable_llm(monkeypatch)
    clock = {"now": 0.0}
    monkeypatch.setattr(rec.time, "monotonic", lambda: clock["now"])
    calls = _stub_source(
        monkeypatch,
        favorites=[{"title": "a"}, {"title": "b"}, {"title": "c"}],
        found={f"新歌 {index}": f"ne_{index}" for index in range(1, 6)},
        top=[{"id": "hot_1"}],
    )
    plain = rec.music_source.search_tracks

    def slow(base_url: str, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        clock["now"] += 9.0  # 每次搜索都超过整个预算
        return plain(base_url, query, **kwargs)

    monkeypatch.setattr(rec.music_source, "search_tracks", slow)

    items, engine = rec.recommend(
        "http://x",
        "u1",
        limit=5,
        ask=lambda seeds, limit: [f"新歌 {index}" for index in range(1, 6)],
    )

    assert engine == "llm"
    assert calls["search"] == 1, "第一首之后就到点了"
    assert items[0]["id"] == "ne_1"
    assert items[-1]["id"] == "hot_1", "剩下的位置用热歌榜补齐"
