"""宠物对话、专家路由与动作建议。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.config import MAX_CHAT_MESSAGE_LENGTH
from app.schemas.common import ContentType
from app.schemas.content import ContentItem

Expert = Literal["image", "bilibili", "game", "fact", "general"]
Live2DAction = Literal["idle", "happy", "think", "remind", "confused", "tap"]
ChatRole = Literal["user", "assistant"]


class PetChatContext(BaseModel):
    content_type: ContentType | None = None
    content_id: str | None = None


class PetChatRequest(BaseModel):
    conversation_id: str | None = Field(default=None, description="缺省时服务端新建会话并返回")
    message: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_LENGTH)
    context: PetChatContext | None = Field(
        default=None,
        description="当前页面上下文，用于把「这张图」解析为具体内容",
    )


class PetChatResponse(BaseModel):
    conversation_id: str
    message_id: str
    reply: str
    expert: Expert
    intent: str
    suggestions: list[ContentItem]
    live2d_action: Live2DAction


class Conversation(BaseModel):
    id: str
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int = Field(ge=0)


class ChatMessage(BaseModel):
    id: str
    role: ChatRole
    content: str
    created_at: datetime
    expert: Expert | None = None
    live2d_action: Live2DAction | None = None
    suggestions: list[ContentItem] = Field(default_factory=list)


class ConversationPage(BaseModel):
    items: list[Conversation]


class MessagePage(BaseModel):
    items: list[ChatMessage]


