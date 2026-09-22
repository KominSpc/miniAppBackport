"""健康检查与模拟来源页。"""

from __future__ import annotations

import html
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.api.v1.common import error_responses
from app.core import envelope
from app.core.timeutil import TIMEZONE_NAME, now
from app.deps import BaseUrlDep, FlagsDep, SettingsDep
from app.schemas.envelopes import EnvelopeHealth
from app.schemas.health import Health, MockSwitchState
from app.services import catalog
from app.services.pixiv import source as pixiv_source

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    operation_id="getHealth",
    response_model=EnvelopeHealth,
    summary="健康检查与当前模拟开关",
    responses=error_responses(500),
)
def get_health(settings: SettingsDep, flags: FlagsDep):
    # 真实内容源启用但登录态缺失或最近一次上游调用失败 → degraded（见 7.4 第 2 条）。
    # 这里不做主动探测：/health 必须便宜，上游状态由最近一次真实请求记录。
    degraded = pixiv_source.degraded()

    health = Health(
        status="degraded" if degraded else "ok",
        version=settings.service_version,
        time=now(),
        timezone=TIMEZONE_NAME,
        mock=MockSwitchState(**flags.as_dict()),
    )
    return envelope.ok(health)


_SOURCE_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - 模拟来源页</title>
<style>
 body{{margin:0;padding:24px;background:#160f1c;color:#f5e9f2;
      font-family:system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif}}
 .card{{max-width:720px;margin:0 auto;background:rgba(255,255,255,.06);
        border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:20px}}
 img{{width:100%;border-radius:12px;display:block;margin:16px 0}}
 .tag{{display:inline-block;margin:2px 6px 2px 0;padding:2px 10px;border-radius:999px;
       background:rgba(255,122,180,.22);font-size:13px}}
 .note{{color:#c9b8c6;font-size:13px;line-height:1.7}}
 a{{color:#ff9ec7}}
</style></head><body><div class="card">
<h1>{title}</h1>
<p class="note">{subtitle}</p>
{tags}
<img src="{cover}" alt="示例图">
<p class="note">{note}</p>
<p><a href="{original}">查看原图</a></p>
</div></body></html>
"""


@router.get("/mock/source/{content_id}", include_in_schema=False, response_class=HTMLResponse)
def mock_source_page(content_id: str, base_url: BaseUrlDep) -> HTMLResponse:
    """占位来源页：模拟期内容没有真实上游页面，这里给一个本服务自有的说明页。"""
    item = catalog.item_by_id(content_id, base_url=base_url)
    if item is None:
        body = _SOURCE_TEMPLATE.format(
            title="内容不存在",
            subtitle=f"未找到 {html.escape(content_id)}",
            tags="",
            cover=catalog.file_url(base_url, "img_0001", "thumb"),
            original=catalog.file_url(base_url, "img_0001", "original"),
            note="模拟服务的条目 ID 形如 img_0001 / video_0001 / game_0001。",
        )
        return HTMLResponse(body, status_code=404)
    tags = "".join(f'<span class="tag">{html.escape(tag)}</span>' for tag in item["tags"])
    title = html.escape(item["title"])
    body = _SOURCE_TEMPLATE.format(
        title=title,
        subtitle=html.escape(item.get("subtitle") or ""),
        tags=tags,
        cover=html.escape(item["cover_url"]),
        original=html.escape(catalog.file_url(base_url, content_id, "original")),
        note="本页由模拟服务生成：图片为程序化绘制的占位图，元数据为编造的示例数据，不代表任何真实作品或作者。",
    )
    return HTMLResponse(body)


def static_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "static"
