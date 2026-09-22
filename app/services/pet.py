"""宠物对话编排：会话管理 + 专家建议 + 回复组装。"""

from __future__ import annotations

from typing import Any

from app.agents import chat
from app.core.timeutil import now, today
from app.repositories import store
from app.repositories.memory import new_id
from app.services import catalog, daily, interaction
from app.services.pet_llm import service as pet_llm

MAX_SUGGESTIONS = 4


def _suggestions(
    decision: chat.RouteDecision,
    *,
    base_url: str,
    context: dict[str, Any] | None,
    safe_mode: bool,
    user_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    favorited = interaction.favorited_ids(user_id)
    intent = decision.intent
    facts: dict[str, Any] = {}

    if intent == "context_image":
        items: list[dict[str, Any]] = []
        content_id = (context or {}).get("content_id")
        if content_id:
            current = catalog.item_by_id(content_id, base_url=base_url, favorited=content_id in favorited, safe_mode=safe_mode)
            if current is not None:
                items.append(current)
                facts["context_title"] = current["title"]
                tag = current["tags"][0] if current["tags"] else None
                for candidate in catalog.filter_by_tag(catalog.all_images(base_url, favorited_ids=favorited, safe_mode=safe_mode), tag):
                    if candidate["id"] != content_id:
                        items.append(candidate)
                    if len(items) >= MAX_SUGGESTIONS:
                        break
        if not items:
            items = catalog.daily_images(base_url, favorited_ids=favorited, safe_mode=safe_mode)[:MAX_SUGGESTIONS]
        return items, facts

    if intent == "search_image":
        query = decision.query or ""
        facts["query"] = query
        items = catalog.search_images(base_url, query, favorited_ids=favorited, safe_mode=safe_mode)[:MAX_SUGGESTIONS] if query else []
        facts["count"] = len(catalog.search_images(base_url, query, favorited_ids=favorited, safe_mode=safe_mode)) if query else 0
        return items, facts

    if intent == "recommend_image":
        return catalog.daily_images(base_url, favorited_ids=favorited, safe_mode=safe_mode)[:MAX_SUGGESTIONS], facts

    if intent == "bilibili_daily":
        return catalog.all_videos(base_url, safe_mode=safe_mode)[:MAX_SUGGESTIONS], facts

    if intent == "game_today":
        items = catalog.today_games(base_url, safe_mode=safe_mode)
        facts["count"] = len(items)
        return items[:MAX_SUGGESTIONS], facts

    if intent == "game_search":
        items = catalog.all_games(base_url, safe_mode=safe_mode)
        facts["count"] = len(items)
        return items[:MAX_SUGGESTIONS], facts

    if intent == "fact_today":
        row = daily.fact_for(today())
        facts["fact_content"] = row["content"]
        return [catalog.build_fact_card(row, base_url=base_url)], facts

    return [], facts


def handle_chat(
    user_id: str,
    *,
    message: str,
    conversation_id: str | None,
    context: dict[str, Any] | None,
    base_url: str,
    safe_mode: bool,
    persona: Any = None,
    touched_part: str | None = None,
) -> dict[str, Any]:
    # 触碰触发的一轮：标题不能拿那句合成提示词（「用户伸手摸了摸你的头」），
    # 它在聊天列表里会显得莫名其妙。
    touch_only = bool(touched_part)
    conversation = store.get_conversation(user_id, conversation_id) if conversation_id else None
    if conversation is None:
        title = f"摸摸{touched_part}" if touch_only else message.strip()[:24]
        conversation = store.create_conversation(user_id, title=title)
    conversation_id = conversation["id"]
    # 历史必须在写入本轮提问之前取：模型要看到的是「之前说过什么」，
    # 把用户刚发的这句也塞进历史等于说了两遍。
    history = store.list_messages(user_id, conversation_id)

    user_message = {
        "id": new_id("msg"),
        "role": "user",
        "content": message,
        "created_at": now(),
        "expert": None,
        "live2d_action": None,
        "suggestions": [],
    }
    # 摸一下宠物不是「用户说的话」：这一轮不写进聊天记录，只在本次请求里作为
    # user 轮次发给模型（部位名走 touched_part → system prompt）。否则用户每摸一次，
    # 对话里就多出一句自己没发过的「（用户伸手摸了摸你的头）」。
    if not touch_only:
        store.append_message(user_id, conversation_id, user_message)

    decision = chat.decide(message, context)
    suggestions, facts = _suggestions(
        decision,
        base_url=base_url,
        context=context,
        safe_mode=safe_mode,
        user_id=user_id,
    )
    # 规则引擎先给出确定性回复：LLM 不可用或两次质检都不过时就用它，
    # 聊天因此不会因为模型抽风而中断。
    fallback = chat.compose_reply(decision, facts)
    # 隐藏好感度：读当前值传进去，模型在回复末尾附一个增减标记，这里累加回库。
    # 规则引擎路径不产出标记，但当前分数照样回报（客户端不显示，测试与调试要看）。
    affection = store.get_affection(user_id)
    outcome = pet_llm.compose(
        fallback=fallback,
        message=message,
        persona=persona,
        history=history,
        touched_part=touched_part,
        facts=facts,
        affection=affection,
    )
    reply = outcome["reply"]
    delta = int(outcome.get("affection_delta") or 0)
    affection_now = store.add_affection(user_id, delta) if delta else affection

    assistant_message = {
        "id": new_id("msg"),
        "role": "assistant",
        "content": reply,
        "created_at": now(),
        "expert": decision.expert,
        "live2d_action": decision.live2d_action,
        "suggestions": suggestions,
    }
    store.append_message(user_id, conversation_id, assistant_message)

    return {
        "conversation_id": conversation_id,
        "message_id": assistant_message["id"],
        "reply": reply,
        "expert": decision.expert,
        "intent": decision.intent,
        "suggestions": suggestions,
        "live2d_action": decision.live2d_action,
        "engine": outcome["engine"],
        "persona_id": outcome["persona_id"],
        "affection": affection_now,
        "actions": outcome.get("actions") or [],
    }


def list_conversations(user_id: str) -> list[dict[str, Any]]:
    return store.list_conversations(user_id)


def list_messages(user_id: str, conversation_id: str) -> list[dict[str, Any]] | None:
    if store.get_conversation(user_id, conversation_id) is None:
        return None
    return store.list_messages(user_id, conversation_id)

