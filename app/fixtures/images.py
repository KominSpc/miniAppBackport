"""示例图片：本地程序化绘制，不使用任何第三方素材。

契约要求 cover_url / thumbnail_url / image_url 指向本服务同源地址
（i.pximg.net 强校验 Referer，客户端直连必然 403），因此模拟期由本模块生成占位图，
并由 /v1/images/{id}/file 统一代理输出。
"""

from __future__ import annotations

import colorsys
import hashlib
import math
import random
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from app.fixtures.dataset import load_fixtures

STATIC_DIR = Path(__file__).resolve().parent.parent / "static" / "images"

THUMB_LONG_EDGE = 400
REGULAR_LONG_EDGE = 900
ORIGINAL_MAX_LONG_EDGE = 1600
JPEG_QUALITY = 82

VIDEO_BASE = (1280, 720)
GAME_BASE = (1024, 1024)

RGB = tuple[int, int, int]

_index: dict[str, dict[str, Any]] | None = None


def content_index() -> dict[str, dict[str, Any]]:
    """全部可出图的条目（图片 + 视频封面 + 游戏封面）。"""
    global _index
    if _index is None:
        fixtures = load_fixtures()
        table: dict[str, dict[str, Any]] = {}
        for item in fixtures.images:
            table[item["id"]] = {
                "kind": "image",
                "width": item["width"],
                "height": item["height"],
                "pages": item["pages"],
            }
        for item in fixtures.videos:
            table[item["id"]] = {"kind": "video", "width": VIDEO_BASE[0], "height": VIDEO_BASE[1], "pages": 1}
        for item in fixtures.games:
            table[item["id"]] = {"kind": "game", "width": GAME_BASE[0], "height": GAME_BASE[1], "pages": 1}
        _index = table
    return _index


def file_name(content_id: str, variant: str, page: int) -> str:
    if variant == "original":
        return f"{content_id}_original_{page}.jpg"
    return f"{content_id}_{variant}.jpg"


def variant_size(content_id: str, variant: str, page: int) -> tuple[int, int]:
    entry = content_index()[content_id]
    width = entry["width"]
    height = entry["height"]
    if variant == "original":
        return _scaled(width, height, ORIGINAL_MAX_LONG_EDGE)
    long_edge = THUMB_LONG_EDGE if variant == "thumb" else REGULAR_LONG_EDGE
    return _scaled(width, height, long_edge)


def _scaled(width: int, height: int, long_edge: int) -> tuple[int, int]:
    longest = max(width, height)
    if longest <= long_edge:
        return width, height
    ratio = long_edge / longest
    return max(1, round(width * ratio)), max(1, round(height * ratio))


def page_count(content_id: str) -> int:
    return int(content_index()[content_id]["pages"])


def image_path(content_id: str, variant: str, page: int) -> Path | None:
    """返回文件路径；条目不存在、页码越界或文件缺失时返回 None。"""
    entry = content_index().get(content_id)
    if entry is None:
        return None
    if variant == "original":
        if page < 0 or page >= entry["pages"]:
            return None
    path = STATIC_DIR / file_name(content_id, variant, page)
    return path if path.exists() else None


# --- 绘制 ---


def _palette(seed_text: str) -> tuple[RGB, RGB, RGB]:
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    hue = digest[0] / 255.0
    specs = ((0.0, 0.52, 0.95), (0.10, 0.60, 0.74), (0.53, 0.42, 0.60))
    colors = []
    for offset, saturation, value in specs:
        r, g, b = colorsys.hsv_to_rgb((hue + offset) % 1.0, saturation, value)
        colors.append((int(r * 255), int(g * 255), int(b * 255)))
    return colors[0], colors[1], colors[2]


def _lerp(a: RGB, b: RGB, t: float) -> RGB:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _gradient(width: int, height: int, top: RGB, bottom: RGB) -> Image.Image:
    strip = Image.new("RGB", (1, height))
    for y in range(height):
        strip.putpixel((0, y), _lerp(top, bottom, y / max(1, height - 1)))
    return strip.resize((width, height), Image.Resampling.BILINEAR)


def render(content_id: str, variant: str, page: int, size: tuple[int, int]) -> Image.Image:
    width, height = size
    seed_text = f"{content_id}:{variant}:{page}"
    rng = random.Random(seed_text)
    first, second, third = _palette(content_id)
    canvas = _gradient(width, height, first, second)

    blobs = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    blob_draw = ImageDraw.Draw(blobs)
    unit = min(width, height)
    for _ in range(rng.randint(5, 9)):
        radius = rng.uniform(0.14, 0.40) * unit
        cx = rng.uniform(0, width)
        cy = rng.uniform(0, height)
        color = rng.choice((first, second, third))
        blob_draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=(*color, rng.randint(40, 92)),
        )
    blobs = blobs.filter(ImageFilter.GaussianBlur(radius=unit * 0.03))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), blobs)

    draw = ImageDraw.Draw(canvas, "RGBA")
    for _ in range(rng.randint(2, 4)):
        cx = rng.uniform(0, width)
        cy = rng.uniform(0, height)
        radius = rng.uniform(0.18, 0.34) * unit
        points = []
        sides = rng.choice((3, 4, 5, 6))
        rotation = rng.uniform(0, 6.283)
        for step in range(sides):
            angle = rotation + step * 6.283 / sides
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        draw.polygon(points, fill=(*third, rng.randint(28, 62)))

    band = int(unit * 0.06)
    offset = rng.randint(0, max(1, height))
    draw.line([(0, offset), (width, offset + band * 2)], fill=(255, 255, 255, 46), width=band)

    step = max(12, unit // 16)
    for gx in range(step, width, step):
        for gy in range(step, height, step):
            radius = 1 + (gx + gy) % 2
            draw.ellipse((gx - radius, gy - radius, gx + radius, gy + radius), fill=(255, 255, 255, 60))

    return canvas.convert("RGB")


def ensure_sample_images(*, force: bool = False, verbose: bool = False) -> int:
    """补齐缺失的示例图，返回本次写入的文件数。"""
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for content_id, entry in content_index().items():
        pages = entry["pages"]
        for variant in ("thumb", "regular", "original"):
            page_range = range(pages) if variant == "original" else range(1)
            for page in page_range:
                path = STATIC_DIR / file_name(content_id, variant, page)
                if path.exists() and not force:
                    continue
                size = variant_size(content_id, variant, page)
                image = render(content_id, variant, page, size)
                image.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True)
                written += 1
                if verbose:
                    print(f"生成 {path.name} {size[0]}x{size[1]}")
    return written

