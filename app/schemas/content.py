"""统一内容模型 ContentItem 与三类 payload 扩展。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ContentType, Source


class ImagePayload(BaseModel):
    author: str
    thumbnail_url: str
    image_url: str
    width: int | None = Field(default=None, description="原图宽度，瀑布流布局必需")
    height: int | None = None
    aspect_ratio: float | None = None
    is_favorited: bool | None = Field(default=None, description="当前匿名用户的收藏状态")
    illust_type: int | None = Field(
        default=None,
        description="Pixiv illustType：0 插画 / 1 漫画 / 2 动图（うごイラ）；非 pixiv 源为 null",
    )
    created_at: datetime | None = None
    user_id: str | None = Field(
        default=None, description="Pixiv 作者 ID（userId）；非 pixiv 源为 null"
    )
    author_avatar: str | None = Field(
        default=None, description="作者头像地址；pixiv 列表接口顺手带回"
    )
    page_count: int | None = Field(
        default=None, description="多图作品的页数（pixiv pageCount）"
    )
    like_count: int | None = Field(default=None, description="点赞数；列表接口通常为空")
    bookmark_count: int | None = Field(default=None, description="收藏数")
    view_count: int | None = Field(default=None, description="浏览数")
    comment_count: int | None = Field(default=None, description="评论数")


class VideoPayload(BaseModel):
    bvid: str
    uploader: str
    play_count: int = Field(ge=0)
    published_at: datetime
    hot_score: float | None = None
    is_hot: bool | None = None
    duration: int | None = Field(default=None, description="时长（秒）")


class GamePayload(BaseModel):
    platforms: list[str]
    genres: list[str]
    updated_at: datetime
    description: str
    version: str | None = None
    developer: str | None = None
    rating: float | None = Field(default=None, ge=0, le=10)
    today_updated: bool | None = None


class CardPayload(BaseModel):
    target_type: ContentType
    target_id: str


class ContentItem(BaseModel):
    id: str = Field(examples=["img_0001"])
    type: ContentType
    title: str
    subtitle: str | None = Field(default=None, description="副标题，视频为 UP 主、游戏为平台串等")
    cover_url: str = Field(description="列表封面，本服务同源地址")
    tags: list[str]
    source: Source
    source_url: str = Field(description="来源页链接，供「查看来源」使用")
    payload: dict[str, Any] = Field(
        description="按 type 取 ImagePayload / VideoPayload / GamePayload / CardPayload",
    )
    is_sensitive: bool = Field(description="敏感内容标记；默认过滤，客户端不得在 safe_mode 下展示")


class ContentItemPage(BaseModel):
    items: list[ContentItem]


class ContentItemList(BaseModel):
    items: list[ContentItem]


class UgoiraFrame(BaseModel):
    """动图的一帧。"""

    file: str = Field(description="帧文件名，与 zip 内条目名一致")
    delay_ms: int = Field(ge=0, description="这一帧的停留时长（毫秒）")


class UgoiraMeta(BaseModel):
    """pixiv 动图（うごイラ）的播放信息。

    动图不是视频：上游给的是一个 zip（内含按序排列的 JPEG 帧）加逐帧延时，
    客户端必须按 ``frames`` 的顺序与延时自己播放。因此这里只描述「怎么播」，
    帧数据由 ``/{id}/ugoira/file`` 单独下发。
    """

    id: str
    frame_count: int = Field(ge=0)
    frames: list[UgoiraFrame]
    total_ms: int = Field(ge=0, description="一轮播放的总时长")
    src: str = Field(description="上游 600x600 zip 地址（仅用于排查，客户端取不到它）")
    original_src: str = Field(description="上游原始分辨率 zip 地址（同上）")
    archive_url: str = Field(description="本服务的 zip 代理地址")


class FavoriteSnapshot(BaseModel):
    """收藏时由客户端带回的条目快照（直连 pixiv 模式专用）。"""

    item: dict[str, Any] = Field(description="客户端当前展示的 ContentItem，服务端只取内容不信任 ID")


class FavoriteState(BaseModel):
    id: str
    is_favorited: bool
    favorite_count: int | None = Field(default=None, ge=0, description="该内容被收藏的总次数，仅用于展示")
