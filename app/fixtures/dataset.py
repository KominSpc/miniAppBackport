"""运行时夹具加载。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class FixtureSet:
    images: tuple[dict[str, Any], ...]
    videos: tuple[dict[str, Any], ...]
    games: tuple[dict[str, Any], ...]
    facts: tuple[dict[str, Any], ...]

    @property
    def known_ids(self) -> frozenset[str]:
        return frozenset(
            item["id"] for item in (*self.images, *self.videos, *self.games)
        )


def _read(name: str) -> tuple[dict[str, Any], ...]:
    path = DATA_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"缺少夹具 {path}，请先执行 python scripts/generate_fixtures.py"
        )
    return tuple(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def load_fixtures() -> FixtureSet:
    return FixtureSet(
        images=_read("images.json"),
        videos=_read("videos.json"),
        games=_read("games.json"),
        facts=_read("facts.json"),
    )
