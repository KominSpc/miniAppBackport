"""宠物对话服务：安全过滤 → 专家路由 → 回复与动作建议。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents import router, safety

LIVE2D_BY_INTENT: dict[str, str] = {
    "greeting": "happy",
    "thanks": "happy",
    "help": "idle",
    "search_image": "think",
    "recommend_image": "think",
    "context_image": "tap",
    "bilibili_daily": "think",
    "game_today": "happy",
    "game_search": "think",
    "fact_today": "remind",
    "unknown": "confused",
    "blocked": "confused",
}


@dataclass(frozen=True)
class RouteDecision:
    expert: str
    intent: str
    query: str | None
    blocked: bool = False
    live2d_action: str = "idle"


def decide(message: str, context: dict[str, Any] | None = None) -> RouteDecision:
    category = safety.detect_blocked_topic(message)
    if category is not None:
        return RouteDecision(expert="general", intent="blocked", query=None, blocked=True, live2d_action="confused")
    expert, intent = router.route(message, context)
    query = router.extract_query(message) if intent in {"search_image", "game_search"} else None
    return RouteDecision(
        expert=expert,
        intent=intent,
        query=query,
        live2d_action=LIVE2D_BY_INTENT.get(intent, "idle"),
    )


def compose_reply(decision: RouteDecision, facts: dict[str, Any]) -> str:
    """facts 由调用方准备好，避免对话层直接依赖仓储。"""
    intent = decision.intent
    if intent == "blocked":
        return safety.safe_reply()
    if intent == "greeting":
        return "在的呀～今天想看点什么？我可以帮你找美图、翻 B 站热门，或者讲个冷知识。"
    if intent == "thanks":
        return "不客气～随时叫我。"
    if intent == "help":
        return "你可以这样说：「找几张猫耳的图」「今天有什么新游戏」「讲个冷知识」。"
    if intent == "context_image":
        title = facts.get("context_title")
        if title:
            return f"你说的是《{title}》这张吧？我把它和相似的几张放在一起了。"
        return "我把相关的几张图整理出来了，看看有没有喜欢的。"
    if intent == "search_image":
        query = decision.query or facts.get("query")
        count = facts.get("count", 0)
        if not query:
            return "我可以帮你按角色或标签找图，说个名字试试？"
        if count == 0:
            return f"「{query}」这边暂时没有合适的图，换个标签试试？"
        return f"关于「{query}」我找到 {count} 张，看看合不合口味～"
    if intent == "recommend_image":
        return "给你挑了几张今天的推荐图，慢慢看～"
    if intent == "bilibili_daily":
        return "今天 B 站的热门里有这几个，我按热度排好了。"
    if intent == "game_today":
        count = facts.get("count", 0)
        return f"今天有 {count} 款游戏更新了，要不要看看？"
    if intent == "game_search":
        return "按平台或类型都能筛，我把全部游戏列出来了。"
    if intent == "fact_today":
        content = facts.get("fact_content")
        if content:
            return f"今天的冷知识：{content}"
        return "今天的冷知识还在路上，稍后再来看看～"
    return "这个我还没学会…换个说法试试？比如「找几张猫耳的图」。"

