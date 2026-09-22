r"""Pixiv 真实接口冒烟：榜单 → 详情 → 图片字节（只读）。

用法（需网络与代理）::

    $env:PIXIV_PROXY = "http://127.0.0.1:7897"
    .venv\Scripts\python.exe scripts\pixiv_smoke.py --mode daily --limit 3

只读、低量：一次榜单请求 + 至多 N 次详情/链接请求 + 1 次图片回源。写入的只有
stdout，不修改任何上游状态。凭据来自 PIXIV_COOKIE（可留空，核心接口未登录可用）。

``--check-filters`` 只做筛选诊断，不发详情请求：对三种 mode/type 组合各查一页，
打印上游返回的 ``xRestrict`` / ``illustType`` 分布，用来判断 R18 / 动图筛选是否
真的生效（详见 docs/EXECUTION_PLAN.md 15.x）::

    $env:PIXIV_PROXY = "http://127.0.0.1:7897"
    .venv\Scripts\python.exe scripts\pixiv_smoke.py --check-filters --keyword "初音ミク"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.services.pixiv import adapter  # noqa: E402
from app.services.pixiv.client import PixivClient  # noqa: E402
from urllib.parse import quote  # noqa: E402


def _distribution(items: list[dict[str, Any]], key: str) -> dict[Any, int]:
    result: dict[Any, int] = {}
    for item in items:
        value = item.get(key)
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items(), key=lambda pair: str(pair[0])))


def check_filters(client: PixivClient, keyword: str, pages: int) -> int:
    """诊断 R18 / 动图筛选在当前登录态下是否真的生效。

    未登录时 pixiv 会忽略 ``mode`` 与 ``type``（实测三种 mode 返回完全一致），
    因此这里直接把上游返回的 ``xRestrict`` / ``illustType`` 分布打出来：配置
    ``PIXIV_COOKIE`` 之前，R18 打开也不会有 ``xRestrict=1`` 的条目。
    """
    print(f"[筛选诊断] keyword={keyword}  登录态={'有' if client.configured else '无'}")
    if not client.configured:
        print("  提示：PIXIV_COOKIE 为空 → 上游按游客处理，mode=r18/all 会被忽略。")
    conditions: tuple[tuple[str, str, str], ...] = (
        ("safe", "all", "全年龄"),
        ("all", "all", "允许 R18"),
        ("safe", "ugoira", "动图（上游 type）"),
    )
    for mode, type_, label in conditions:
        envelope = client.get_json(
            f"/ajax/search/artworks/{quote(keyword, safe='')}",
            params={
                "mode": mode,
                "p": 1,
                "s_mode": "s_tag",
                "type": type_,
                "lang": "zh",
                "order": "date_d",
            },
        )
        entries, total = adapter.search_illusts(envelope)
        if pages > 1:
            print(f"    （只取第一页；上游 total={total}）")
        print(
            f"  mode={mode:<5} type={type_:<7} {label} → {len(entries)} 条 "
            f"xRestrict={_distribution(entries, 'xRestrict')} "
            f"illustType={_distribution(entries, 'illustType')}"
        )
    print("  结论：R18 依赖登录态（PIXIV_COOKIE 配好后 mode=all 才会出现 xRestrict=1）；")
    print("        动图不依赖登录态，服务端已按 illustType==2 本地过滤。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pixiv 适配器冒烟")
    parser.add_argument("--mode", default="daily", choices=("daily", "weekly", "monthly", "rookie"))
    parser.add_argument("--limit", type=int, default=3, help="打印条数")
    parser.add_argument("--no-image", action="store_true", help="跳过图片字节回源")
    parser.add_argument("--check-filters", action="store_true", help="只诊断 R18 / 动图筛选是否生效")
    parser.add_argument("--keyword", default="初音ミク", help="诊断用检索词")
    args = parser.parse_args()

    settings = load_settings()
    client = PixivClient(settings)
    print(f"proxy={settings.pixiv_proxy or '(直连)'}  login_state={'有' if client.configured else '无'}")

    try:
        if args.check_filters:
            return check_filters(client, args.keyword, args.limit)
        envelope = client.get_json(
            "/ranking.php", params={"mode": args.mode, "content": "illust", "format": "json"}
        )
        entries = adapter.ranking_illusts(envelope)
        print(f"[1] ranking mode={args.mode} → {len(entries)} 条")
        for entry in entries[: args.limit]:
            item = adapter.build_illust(entry, base_url="http://127.0.0.1:8000")
            print(
                f"    {item['id']:<14} {item['title'][:28]:<30} "
                f"{item['payload']['width']}x{item['payload']['height']} "
                f"sensitive={item['is_sensitive']}"
            )
        if not entries:
            print("    榜单为空：可能被限流或登录态失效")
            return 1

        pid = str(entries[0].get("illust_id") or entries[0].get("id"))
        detail = client.get_json(f"/ajax/illust/{pid}")
        body = adapter.body_of(detail) or {}
        mapped = adapter.build_illust(body, base_url="http://127.0.0.1:8000")
        print(
            f"[2] /ajax/illust/{pid} → {mapped['title'][:28]} "
            f"pages={mapped['payload']['page_count']} tags={mapped['tags'][:4]}"
        )

        pages = adapter.page_urls(
            client.get_json(f"/ajax/illust/{pid}/pages", params={"lang": "zh"})
        )
        print(f"[3] /pages → {len(pages)} 页")

        if not args.no_image and pages:
            url = adapter.pick_url(pages[0], "thumb")
            content, media_type = client.get_bytes(str(url))
            print(f"[4] pximg 回源 → {len(content)} 字节 {media_type}")
        return 0
    except AppError as error:
        print(f"失败：{error.code} {error.message} {error.details}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())