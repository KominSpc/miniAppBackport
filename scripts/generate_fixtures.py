"""把 fixture_tables.py 的内容导出为运行时读取的 JSON 夹具。

用法：python scripts/generate_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_tables import FACTS, GAMES, IMAGES, VIDEOS  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "app" / "fixtures" / "data"

# 显式标记的敏感条目，用于验证 safe_mode 过滤
SENSITIVE_IDS = {"img_0005", "img_0013", "img_0039"}

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def fake_bvid(seed_text: str) -> str:
    """生成形状合法但内容编造的 BV 号（BV + 10 位）。"""
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    body = "".join(_ALPHABET[b % len(_ALPHABET)] for b in digest[:10])
    return "BV1" + body[1:]


def build_images() -> list[dict]:
    rows = []
    for index, (title, author, tags, width, height, ago_hours, pages) in enumerate(IMAGES, start=1):
        content_id = f"img_{index:04d}"
        rows.append(
            {
                "id": content_id,
                "title": title,
                "author": author,
                "tags": tags,
                "width": width,
                "height": height,
                "aspect_ratio": round(width / height, 4),
                "ago_hours": ago_hours,
                "pages": pages,
                "is_sensitive": content_id in SENSITIVE_IDS,
            }
        )
    return rows


def build_videos() -> list[dict]:
    rows = []
    for index, (title, uploader, tags, play_count, ago_hours, duration, hot_score) in enumerate(VIDEOS, start=1):
        content_id = f"video_{index:04d}"
        rows.append(
            {
                "id": content_id,
                "title": title,
                "uploader": uploader,
                "bvid": fake_bvid(content_id),
                "tags": tags,
                "play_count": play_count,
                "ago_hours": ago_hours,
                "duration": duration,
                "hot_score": hot_score,
                "is_hot": hot_score >= 80,
                "is_sensitive": False,
            }
        )
    return rows


def build_games() -> list[dict]:
    rows = []
    for index, (title, developer, platforms, genres, version, rating, description, ago_hours) in enumerate(GAMES, start=1):
        rows.append(
            {
                "id": f"game_{index:04d}",
                "title": title,
                "developer": developer,
                "platforms": platforms,
                "genres": genres,
                "version": version,
                "rating": rating,
                "description": description,
                "ago_hours": ago_hours,
                "is_sensitive": False,
            }
        )
    return rows


def build_facts() -> list[dict]:
    rows = []
    for index, (content, tags, source_url) in enumerate(FACTS, start=1):
        rows.append(
            {
                "id": f"fact_{index:04d}",
                "content": content,
                "tags": tags,
                "source_url": source_url,
            }
        )
    return rows


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payloads = {
        "images.json": build_images(),
        "videos.json": build_videos(),
        "games.json": build_games(),
        "facts.json": build_facts(),
    }
    for name, data in payloads.items():
        path = OUT_DIR / name
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"{name}: {len(data)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
