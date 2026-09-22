r"""B 站真实接口冒烟：热门榜 / 分类榜 → 封面回源（只读）。

用法（需网络；国内网络通常需要代理）::

    $env:BILIBILI_PROXY = "http://127.0.0.1:7897"
    .venv\Scripts\python.exe scripts\bilibili_smoke.py --limit 3
    .venv\Scripts\python.exe scripts\bilibili_smoke.py --category game --limit 3

只读、低量：默认 2 次热门页 + 至多 1 次封面回源。热门接口足够温和，但
``x/web-interface/ranking/v2`` 对密集请求敏感（实测连打后 ``-352`` 持续约 10 分钟），
所以分类榜只打一次，脚本内还会主动等待一段时间。

写入的只有 stdout，不修改任何上游状态；不携带 Cookie（公开接口不需要）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.services.bilibili import adapter  # noqa: E402
from app.services.bilibili.client import BilibiliClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="B 站适配器冒烟")
    parser.add_argument("--category", default=None, help="分类榜 slug，留空测热门综合榜")
    parser.add_argument("--limit", type=int, default=3, help="打印条数")
    parser.add_argument("--no-image", action="store_true", help="跳过封面字节回源")
    args = parser.parse_args()

    settings = load_settings()
    client = BilibiliClient(settings)
    print(f"proxy={settings.bilibili_proxy or '(直连)'}  无需登录态")

    try:
        if args.category:
            rid = adapter.category_rid(args.category)
            if rid is None:
                print(f"未知分区 slug：{args.category}（可选：{', '.join(adapter.RANKING_CATEGORIES)}）")
                return 1
            time.sleep(2.0)  # 分类榜更严格，先等一会儿
            payload = client.get_json(
                "/x/web-interface/ranking/v2", params={"rid": rid, "type": "all"}
            )
            entries = adapter.video_items(payload)
            print(f"[1] ranking rid={rid}({adapter.category_name(args.category)}) → {len(entries)} 条")
        else:
            payload = client.get_json(
                "/x/web-interface/popular", params={"ps": 20, "pn": 1}
            )
            entries = adapter.video_items(payload)
            print(f"[1] popular pn=1 → {len(entries)} 条")

        if not entries:
            print("    榜单为空：多半是风控（-352）或网络问题")
            return 1

        ordered = sorted(entries, key=adapter.like_rate_of, reverse=True)
        passing = [e for e in ordered if adapter.like_rate_of(e) > settings.bilibili_like_rate_threshold]
        print(f"[2] 点赞率 > {settings.bilibili_like_rate_threshold} 的高质量条目：{len(passing)}/{len(entries)}")
        for entry in ordered[: args.limit]:
            item = adapter.build_video(entry, base_url="http://127.0.0.1:8000")
            payload_map = item["payload"]
            print(
                f"    {item['id']:<16} 点赞率={payload_map['like_rate']:<7.4f} "
                f"播放={payload_map['play_count']:<9} 分区={payload_map['category']:<6} "
                f"{item['title'][:26]}"
            )

        if not args.no_image:
            url = adapter.cover_variant(adapter.cover_url_of(ordered[0]), "thumb")
            content, media_type = client.get_bytes(url)
            print(f"[3] hdslb 封面回源 → {len(content)} 字节 {media_type}")
        return 0
    except AppError as error:
        print(f"失败：{error.code} {error.message} {error.details}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())