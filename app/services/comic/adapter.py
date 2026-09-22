"""把爬虫的原始字段映射成契约里的漫画模型。

上游（ComicDown）两个关键差异都在这里抹平：

1. **封面地址**是协议相对写法（manhuagui 给的是 `//cf.mhgui.com/cpic/h/1128.jpg`，
   浏览器按当前页面协议补，`Image.network` 会直接报错），统一补成 `https:`；
2. **内页图不下发给客户端**：manhuagui 的内页图不带 Referer 就 403，客户端在 web 上
   又发不出 Referer，因此 `image_urls` 一律换成本服务的 `/v1/comics/image` 代理地址。

漫画 ID 用 `站点:上游ID` 的形状（`manhuagui:1128`）：上游每个站点各自一套 ID，
不带站点前缀会撞车。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.config import MOCK_CONTENT_VERSION  # noqa: F401  （仅用于保持导入风格一致）

# 站点展示名。上游 `source_name` 里也有（中文），但 dm5 会带后缀，这里统一口径。
SITE_NAMES: dict[str, str] = {
    "manhuagui": "漫画柜",
    "qq": "腾讯动漫",
    "dm5": "动漫屋",
}

# 站点主页，供「在浏览器里打开」使用
SITE_HOMES: dict[str, str] = {
    "manhuagui": "https://www.manhuagui.com/",
    "qq": "https://ac.qq.com/",
    "dm5": "https://www.dm5.com/",
}


def site_name(site: str) -> str:
    return SITE_NAMES.get(site, site)


def absolutize(url: str | None) -> str:
    """补全协议相对地址；空值原样返回空串。"""
    value = (url or "").strip()
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("http://"):
        # qq 的部分封面写死了 http，安卓 9+ 默认禁止明文，统一升到 https
        return f"https://{value[len('http://'):]}"
    return value


def comic_id(site: str, raw_id: str) -> str:
    return f"{site}:{raw_id}"


def split_comic_id(value: str) -> tuple[str, str] | None:
    """`manhuagui:1128` → ("manhuagui", "1128")；前缀不认识时返回 None。"""
    site, _, raw_id = value.partition(":")
    if not site or not raw_id or site not in SITE_NAMES:
        return None
    return site, raw_id


def cover_url(raw_cover: Any, *, site: str, base_url: str = "") -> str:
    """封面地址 → 本服务的图片代理地址。

    上游封面（`cf.mhgui.com` / `manhua.acimg.cn` / `mhfm2cnc.cdndm5.com`）不返回 CORS
    头，web 上 `Image.network` 拿不到像素；交给本服务转发顺带还能补 https 与 Referer。
    [base_url] 为空（离线夹具、直接调适配器的测试）时退回直链。
    """
    absolute = upgrade_cover(absolutize(raw_cover))
    if not absolute or not base_url:
        return absolute
    return image_proxy_url(base_url, absolute, site=site)


#: manhuagui 的封面档位目录：`b` = 132×176（列表缩略图，最糊）、`h` = 180×240、
#: 不带字母 = 原图（实测 165×240 ~ 360×480）。爬虫给的是最糊的那档。
COVER_THUMB_DIRS: tuple[str, ...] = ("b", "h", "m")

#: 封面原图所在的图床（只有它需要换档，另外两家给的就是能用的尺寸）。
MANHUAGUI_IMAGE_HOST: str = "cf.mhgui.com"


def _cover_parts(url: str) -> tuple[str, str, str] | None:
    """`https://cf.mhgui.com/cpic/b/1128.jpg` → (前缀, 档位目录, 文件名)。

    只认 manhuagui 这一个图床；别的站点（或内页图）返回 None，原样不动。
    """
    prefix, marker, tail = url.partition("/cpic/")
    if not marker or MANHUAGUI_IMAGE_HOST not in prefix:
        return None
    head, _, name = tail.partition("/")
    if not name or "." not in name:
        return None
    return prefix, head, name


def upgrade_cover(url: str) -> str:
    """把封面换成**原图**档位。

    为什么要换：爬虫下发的是 `/cpic/b/` 那档 132×176 的缩略图，铺在手机上是硬放大，
    作者名字都糊成一团（需求里的「漫画预览图太糊了」）。`/cpic/<name>` 是同一张图的
    原图，实测 3/20 的 `_98` / `_85` 后缀封面没有原图档、会 503，所以回源侧保留了
    回落到 `h/`（180×240）的那条路（见 cover_fallback）。
    """
    parts = _cover_parts(url)
    if parts is None:
        return url
    prefix, head, name = parts
    if head not in COVER_THUMB_DIRS:
        return url
    return f"{prefix}/cpic/{name}"


def cover_fallback(url: str) -> str | None:
    """原图档取不到时的退路：`h/`（180×240）；无从回落时返回 None。"""
    parts = _cover_parts(url)
    if parts is None:
        return None
    prefix, head, name = parts
    if head in COVER_THUMB_DIRS:
        # 已经在缩略档位上，再降只会更糊
        return None
    return f"{prefix}/cpic/h/{name}"


def summary(raw: dict[str, Any], *, site: str, base_url: str = "") -> dict[str, Any]:
    raw_id = str(raw.get("comicid") or "")
    return {
        "id": comic_id(site, raw_id),
        "site": site,
        "site_name": site_name(site),
        "comic_id": raw_id,
        "title": str(raw.get("name") or "").strip() or "未命名",
        "cover_url": cover_url(raw.get("cover_image_url"), site=site, base_url=base_url),
        "source_url": str(raw.get("source_url") or SITE_HOMES.get(site, "")),
        "status": str(raw.get("status") or "").strip() or None,
    }


def chapter_refs(raw: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in raw.get("chapters") or []:
        refs.append(
            {
                "number": int(item.get("chapter_number") or 0),
                "title": str(item.get("title") or "").strip(),
                "source_url": str(item.get("source_url") or ""),
            }
        )
    return refs


def chapter_groups(raw: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for group in raw.get("ext_chapters") or []:
        chapters = [
            {
                "number": int(item.get("chapter_number") or 0),
                "title": str(item.get("title") or "").strip(),
                "source_url": str(item.get("source_url") or ""),
            }
            for item in (group.get("chapters") or [])
        ]
        if chapters:
            groups.append({"name": str(group.get("ext_name") or "番外"), "chapters": chapters})
    return groups


def detail(raw: dict[str, Any], *, site: str, base_url: str = "") -> dict[str, Any]:
    base = summary(raw, site=site, base_url=base_url)
    tags = [tag.strip() for tag in str(raw.get("tag") or "").split(",") if tag.strip()]
    chapters = chapter_refs(raw)
    base.update(
        {
            "author": (str(raw.get("author") or "").strip() or None),
            "tags": tags,
            "description": (str(raw.get("description") or raw.get("desc") or "").strip() or None),
            "chapter_count": len(chapters),
            "chapters": chapters,
            "ext_chapters": chapter_groups(raw),
        }
    )
    return base


def chapter_content(
    raw: dict[str, Any],
    *,
    site: str,
    raw_comic_id: str,
    number: int,
    ext_name: str = "",
    base_url: str = "",
) -> dict[str, Any]:
    """章节目录 → 契约形状。内页图换成同源代理地址（见模块文档）。"""
    urls = [absolutize(url) for url in (raw.get("image_urls") or [])]
    proxied = [image_proxy_url(base_url, url, site=site) for url in urls if url]
    return {
        "id": f"{site}:{raw_comic_id}:{number}",
        "site": site,
        "site_name": site_name(site),
        "comic_id": raw_comic_id,
        "number": number,
        "ext_name": ext_name,
        "title": str(raw.get("title") or "").strip(),
        "source_url": str(raw.get("source_url") or ""),
        "image_urls": proxied,
        "image_count": len(proxied),
    }


def image_proxy_url(base_url: str, url: str, *, site: str | None = None) -> str:
    """本服务的图片代理地址：客户端只认它，直链一律不外发。"""
    prefix = f"{base_url.rstrip('/')}/v1/comics/image"
    query = f"url={quote(url, safe='')}"
    if site:
        query = f"{query}&site={quote(site, safe='')}"
    return f"{prefix}?{query}"


def tag_groups(raw: Any) -> list[dict[str, Any]]:
    """爬虫的 tags 形状：[{category, tags: [{name, tag}]}]。"""
    groups: list[dict[str, Any]] = []
    for group in raw or []:
        tags = [
            {"name": str(item.get("name") or ""), "tag": str(item.get("tag") or "")}
            for item in (group.get("tags") or [])
            if item.get("tag")
        ]
        if tags:
            groups.append({"category": str(group.get("category") or "分类"), "tags": tags})
    return groups
