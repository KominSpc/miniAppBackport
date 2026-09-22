"""宠物对话、专家路由与动作建议。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.config import MAX_CHAT_MESSAGE_LENGTH
from app.constants import llm as llm_constants
from app.schemas.common import ContentType
from app.schemas.content import ContentItem

Expert = Literal["image", "bilibili", "game", "fact", "general"]
Live2DAction = Literal["idle", "happy", "think", "remind", "confused", "tap"]
ChatRole = Literal["user", "assistant"]


class PetChatContext(BaseModel):
    content_type: ContentType | None = None
    content_id: str | None = None


class PetPersona(BaseModel):
    """用户在宠物设置页里配置的人设；随每次对话一起发过来。

    服务端不持久化人设：它在客户端可编辑、可切换，发过来就用，没发就用内置的。
    （只有**隐藏好感度**落在服务端：它要跨重启、跨设备跟着同一个用户走。）
    """

    id: str | None = Field(default=None, max_length=64)
    name: str = Field(
        min_length=1,
        max_length=llm_constants.MAX_PERSONA_NAME_LENGTH,
        description="人设名，会出现在设置页的切换列表里",
    )
    prompt: str = Field(
        min_length=1,
        max_length=llm_constants.MAX_PERSONA_PROMPT_LENGTH,
        description="角色设定，作为 system prompt 的一部分转发给 LLM",
    )
    catchphrase: str | None = Field(
        default=None, max_length=llm_constants.MAX_PERSONA_CATCHPHRASE_LENGTH
    )
    scene: str | None = Field(
        default=None,
        max_length=llm_constants.MAX_PERSONA_SCENE_LENGTH,
        description=(
            "可选的「角色细节」：这个人在某个场景里怎么说话、面对某件事是什么反应。"
            "提示词里要求模型据此推断表象性格 / 隐性性格 / 社会假面 / 谈吐，"
            "而不是复述片段。"
        ),
    )
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class PetChatRequest(BaseModel):
    conversation_id: str | None = Field(default=None, description="缺省时服务端新建会话并返回")
    message: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_LENGTH)
    context: PetChatContext | None = Field(
        default=None,
        description="当前页面上下文，用于把「这张图」解析为具体内容",
    )
    persona: PetPersona | None = Field(
        default=None,
        description="本轮使用的人设；缺省用服务端内置人设",
    )
    touched_part: str | None = Field(
        default=None,
        max_length=64,
        description="触碰反馈返回的组件名（水印部件除外），作为本轮上下文发给 LLM",
    )


class PetChatResponse(BaseModel):
    conversation_id: str
    message_id: str
    reply: str
    expert: Expert
    intent: str
    suggestions: list[ContentItem]
    live2d_action: Live2DAction
    engine: Literal["rules", "llm"] = Field(
        default="rules",
        description="这句话由谁生成：规则引擎（含 LLM 不可用时的回落）或 LLM",
    )
    persona_id: str | None = Field(default=None, description="本轮实际使用的人设 id")
    actions: list[str] = Field(
        default_factory=list,
        description=(
            "本轮宠物想做的动作，形如 move:left / move:random / hide / show。"
            "客户端照着执行；不认识的取值直接忽略。"
        ),
    )
    affection: int | None = Field(
        default=None,
        description=(
            "隐藏好感度（0-100）。界面上不展示任何数字，只在服务端用来调节模型说话的"
            "口气；露在响应里是为了调试与测试。"
        ),
    )


class PetTtsRequest(BaseModel):
    """TTS 预留接口的请求体；当前一律返回「未配置」。"""

    text: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_LENGTH)
    voice: str | None = Field(default=None, max_length=64)


class PetTtsResult(BaseModel):
    """TTS 预留接口的响应体（接上供应商后才会真正返回）。"""

    audio_url: str
    mime_type: str = "audio/mpeg"
    voice: str | None = None
    duration_ms: int | None = None


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


