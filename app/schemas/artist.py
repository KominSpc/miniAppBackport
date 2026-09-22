"""pixiv 作者主页的契约模型。

客户端在 web 上直连 pixiv 会被浏览器的同源策略拦下（上游不返回 CORS 头），
因此作者页与作者作品流必须有后端通道；这两个模型就是那条通道的响应形状。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PixivArtist(BaseModel):
    """作者主页信息。"""

    user_id: str = Field(description="pixiv 作者 ID")
    name: str = Field(description="作者昵称")
    avatar_url: str = Field(default="", description="头像地址（上游 i.pximg.net，仅供展示参考）")
    illust_count: int = Field(default=0, description="插画数")
    manga_count: int = Field(default=0, description="漫画数")
    work_count: int = Field(default=0, description="作品总数（插画 + 漫画）")
    page_url: str = Field(description="pixiv 作者主页地址")
