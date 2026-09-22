"""漫画页（漫画柜 / 腾讯动漫 / 动漫屋）的独立模型。

漫画与美图（ContentItem 的四类 payload）形状差得远：漫画有章节表、章节目录、
标签分组，硬塞进 payload 只会让那四种形状失去意义，所以另起一组 schema —— 与
音乐页同一个理由。

ID 形状：漫画 `manhuagui:1128`（站点前缀 + 上游 ID），章节
`manhuagui:1128:12`（再拼章节号）。上游每个站点各自一套 ID，不带站点前缀会撞车。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ComicSummary(BaseModel):
    """列表里的漫画条目。"""

    id: str = Field(description="本服务生成的漫画 ID，形如 manhuagui:1128")
    site: str = Field(description="来源站点：manhuagui / qq / dm5")
    site_name: str = Field(description="站点中文名，直接展示")
    comic_id: str = Field(description="上游站点内的漫画 ID")
    title: str
    cover_url: str = Field(description="封面地址，已补全协议；可能为空串")
    source_url: str = Field(description="上游漫画页，供「查看来源」使用")
    status: str | None = Field(default=None, description="连载状态文案，上游没给就是 null")


class ComicSummaryList(BaseModel):
    items: list[ComicSummary]


class ComicChapterRef(BaseModel):
    number: int = Field(ge=0, description="章节序号，1 起；上游用 0 表示「第 0 话」时照实返回")
    title: str
    source_url: str


class ComicChapterGroup(BaseModel):
    """番外 / 单行本之类的额外分组。"""

    name: str
    chapters: list[ComicChapterRef]


class ComicDetail(ComicSummary):
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    chapter_count: int = Field(ge=0)
    chapters: list[ComicChapterRef] = Field(default_factory=list)
    ext_chapters: list[ComicChapterGroup] = Field(default_factory=list)


class ComicChapterContent(BaseModel):
    """一章的图片列表。图片一律是本服务的同源代理地址，见 services/comic/adapter.py。"""

    id: str = Field(description="形如 manhuagui:1128:1")
    site: str
    site_name: str
    comic_id: str
    number: int = Field(ge=0)
    ext_name: str = Field(default="", description="番外分组名，主篇为空串")
    title: str
    source_url: str
    image_urls: list[str] = Field(default_factory=list)
    image_count: int = Field(ge=0)


class ComicTag(BaseModel):
    name: str = Field(description="展示名")
    tag: str = Field(description="上游取值，回传给 /v1/comics/tag")


class ComicTagGroup(BaseModel):
    category: str
    tags: list[ComicTag]


class ComicTagGroupList(BaseModel):
    items: list[ComicTagGroup]
