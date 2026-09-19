"""宠物对话编排：会话管理 + 专家建议 + 回复组装。"""

from __future__ import annotations

from typing import Any

from app.agents import chat
from app.core.timeutil import now, today
from app.repositories.memory import new_id, store
from app.services import catalog, daily, interaction

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
) -> dict[str, Any]:
    conversation = store.get_conversation(user_id, conversation_id) if conversation_id else None
    if conversation is None:
        conversation = store.create_conversation(user_id, title=message.strip()[:24])
    conversation_id = conversation["id"]

    user_message = {
        "id": new_id("msg"),
        "role": "user",
        "content": message,
        "created_at": now(),
        "expert": None,
        "live2d_action": None,
        "suggestions": [],
    }
    store.append_message(user_id, conversation_id, user_message)

    decision = chat.decide(message, context)
    suggestions, facts = _suggestions(
        decision,
        base_url=base_url,
        context=context,
        safe_mode=safe_mode,
        user_id=user_id,
    )
    reply = chat.compose_reply(decision, facts)

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
    }


def list_conversations(user_id: str) -> list[dict[str, Any]]:
    return store.list_conversations(user_id)


def list_messages(user_id: str, conversation_id: str) -> list[dict[str, Any]] | None:
    if store.get_conversation(user_id, conversation_id) is None:
        return None
    return store.list_messages(user_id, conversation_id)

