"""游戏筛选元数据：平台 / 类型全集与计数。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GameFilterOption(BaseModel):
    """一个筛选项及其命中数量。"""

    value: str = Field(description="筛选值（平台名或类型名），可直接用作 /v1/games 的查询参数")
    count: int = Field(ge=0, description="该取值下的作品数量（按当前用户的安全模式过滤后统计）")


class GameFilters(BaseModel):
    """筛选条的全集：与当前筛选条件无关，客户端据此渲染稳定不变的选项。"""

    platforms: list[GameFilterOption]
    genres: list[GameFilterOption]