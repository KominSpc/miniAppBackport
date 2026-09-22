"""宠物对话、专家路由与动作建议。"""

from __future__ import annotations

from fastapi import APIRouter

from app.agents.safety import normalize
from app.api.v1.common import error_responses
from app.core import envelope
from app.core.errors import bad_request, not_found
from app.core.pagination import paginate
from app.deps import BaseUrlDep, CurrentUser, CursorQuery, LimitQuery
from app.schemas.envelopes import (
    EnvelopeConversationPage,
    EnvelopeMessagePage,
    EnvelopePetChatResponse,
    EnvelopePetTtsResult,
)
from app.schemas.pet import PetChatRequest, PetChatResponse, PetTtsRequest, PetTtsResult
from app.services import interaction, pet
from app.services.pet_llm import tts as pet_tts

router = APIRouter(prefix="/v1/pet", tags=["pet"])


@router.post(
    "/chat",
    operation_id="postPetChat",
    response_model=EnvelopePetChatResponse,
    summary="宠物对话、专家路由与动作建议",
    responses=error_responses(400, 401, 422, 429, 500, 503),
)
def post_pet_chat(payload: PetChatRequest, user: CurrentUser, base_url: BaseUrlDep):
    message = normalize(payload.message)
    if not message:
        raise bad_request({"message": "内容不能为空"})
    context = payload.context.model_dump(exclude_none=True) if payload.context else None
    result = pet.handle_chat(
        user["user_id"],
        message=message,
        conversation_id=payload.conversation_id,
        context=context,
        base_url=base_url,
        safe_mode=interaction.safe_mode_for(user["user_id"]),
        persona=payload.persona,
        touched_part=payload.touched_part,
    )
    return envelope.ok(PetChatResponse(**result))


@router.post(
    "/tts",
    operation_id="postPetTts",
    response_model=EnvelopePetTtsResult,
    summary="语音合成（接口预留，尚未接入供应商）",
    responses=error_responses(400, 401, 422, 429, 500, 503),
)
def post_pet_tts(payload: PetTtsRequest, user: CurrentUser):
    """需求要求「后端准备 tts 的接口（暂时不用）」。

    契约现在就定下来（含 503 的失败语义），客户端据此隐藏播放入口；
    接入供应商后只需要把 ``pet_tts.synthesize`` 实现掉。
    """
    return envelope.ok(PetTtsResult(**pet_tts.synthesize(payload.text, voice=payload.voice)))


@router.get(
    "/conversations",
    operation_id="getPetConversations",
    response_model=EnvelopeConversationPage,
    summary="会话历史",
    responses=error_responses(400, 401, 429, 500),
)
def get_pet_conversations(
    user: CurrentUser,
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
):
    items = pet.list_conversations(user["user_id"])
    page, next_cursor, has_more = paginate(items, scope="pet:conversations", cursor=cursor, limit=limit)
    return envelope.ok({"items": page}, next_cursor=next_cursor, has_more=has_more)


@router.get(
    "/conversations/{id}/messages",
    operation_id="getPetConversationMessages",
    response_model=EnvelopeMessagePage,
    summary="会话消息列表",
    responses=error_responses(400, 401, 404, 429, 500),
)
def get_pet_conversation_messages(
    user: CurrentUser,
    id: str,
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
):
    messages = pet.list_messages(user["user_id"], id)
    if messages is None:
        raise not_found({"conversation_id": id})
    page, next_cursor, has_more = paginate(
        messages, scope=f"pet:messages:{id}", cursor=cursor, limit=limit
    )
    return envelope.ok({"items": page}, next_cursor=next_cursor, has_more=has_more)
