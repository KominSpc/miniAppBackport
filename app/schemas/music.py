"""音乐页（网易云 / QQ 音乐）的独立模型。

音乐不需要 ContentItem 的四类 payload 形状（封面 / 时长 / 歌单 / 评论 / 歌词都是图片、
视频用不到的字段），硬塞进 payload 会让那四种形状失去意义，因此这里另起一组 schema。
曲目 ID 由 ``{平台前缀}_{上游歌曲 ID}`` 组成，见 ``app/services/music/adapter.py``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# 音质由高到低：请求最高一档，上游拿不到就顺着这张表往下退。
MUSIC_LEVELS: tuple[str, ...] = ("jymaster", "hires", "lossless", "exhigh", "standard")
MusicLevel = Literal["jymaster", "hires", "lossless", "exhigh", "standard"]


class MusicTrack(BaseModel):
    id: str = Field(description="本服务生成的曲目 ID，形如 ne_3362265838（平台前缀 + 上游 ID）")
    platform: str = Field(description="音乐平台：netease / qqmusic")
    source_id: str = Field(description="上游歌曲 ID")
    title: str
    artists: list[str]
    album: str | None = None
    duration_ms: int = Field(ge=0)
    cover_url: str = Field(description="封面，本服务同源地址")
    source_url: str = Field(description="上游歌曲页链接，供「查看来源」使用")
    fee: int | None = Field(
        default=None,
        description="上游付费标记：0 免费 / 1 会员 / 8 低音质试听；未登录时会员曲目只有试听片段",
    )
    is_favorited: bool | None = Field(default=None, description="当前匿名用户是否已收藏到收藏夹")
    kind: Literal["song", "voice", "radio"] = Field(
        default="song",
        description=(
            "内容形态：song 单曲 / voice 声音（播客单集）/ radio 电台节目。"
            "后两者是搜索时从网易云的「声音」「电台」里合并进来的，播放链路与单曲一致"
        ),
    )


class MusicTrackList(BaseModel):
    items: list[MusicTrack]


class MusicPlayback(BaseModel):
    """一次播放解析的结果。客户端据此决定展示「试听」还是完整播放。"""

    id: str
    stream_url: str = Field(description="本服务同源的音频流地址，支持 Range 请求（可拖动进度条）")
    mime_type: str
    bitrate: int = Field(ge=0, description="上游实际给出的码率，未登录时恒为 128kbps mp3")
    duration_ms: int = Field(ge=0)
    requested_level: str = Field(description="请求的音质档位")
    effective_level: str = Field(description="上游实际返回的档位，可能低于 requested_level")
    is_trial: bool = Field(description="true 表示只有试听片段（会员曲目且未登录）")
    trial_end_ms: int | None = None


class MusicLyric(BaseModel):
    id: str
    lyric: str
    translated_lyric: str | None = None


class MusicComment(BaseModel):
    id: str
    author: str
    content: str
    liked_count: int = Field(ge=0)
    created_at: datetime
    location: str | None = None


class MusicCommentList(BaseModel):
    items: list[MusicComment]
    total: int = Field(ge=0)


class MusicFavoriteState(BaseModel):
    id: str
    is_favorited: bool


class MusicPlaylist(BaseModel):
    """一个收藏夹（歌单）。用户可以建多个，默认歌单兜底旧的单收藏夹接口。"""

    id: str = Field(description="歌单 ID，形如 pl_ab12cd34ef56")
    name: str
    track_count: int = Field(ge=0, description="歌单里的曲目数")
    track_ids: list[str] = Field(
        description=(
            "歌单内的曲目 ID（按加入先后）。客户端据此判断某首歌在哪些歌单里，"
            "不必逐个歌单回源查询。"
        )
    )
    is_default: bool = Field(description="默认收藏夹：不可删除，旧版 /favorites 接口的落点")
    created_at: datetime


class MusicPlaylistList(BaseModel):
    items: list[MusicPlaylist]


class MusicPlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40, description="歌单名")


class MusicPlaylistState(BaseModel):
    """把一首歌加入 / 移出一个歌单的结果。"""

    playlist_id: str
    track_id: str
    in_playlist: bool = Field(description="本次操作后这首歌是否在该歌单里")
    track_count: int = Field(ge=0, description="操作后歌单里的曲目数")
