"""专家路由：把用户的话映射到 image / bilibili / game / fact / general。

路线顺序固定为「上下文 > 打招呼与致谢 > 关键词打分 > 兜底」，
命中结果同时给出 intent，供回复模板与动作建议使用。
"""

from __future__ import annotations

from typing import Any

from app.agents.safety import normalize

EXPERT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "image": ("图", "美图", "插画", "壁纸", "画师", "原图", "pixiv", "封面", "头像", "同人图"),
    "bilibili": ("视频", "b站", "bilibili", "番剧", "新番", "up主", "投稿", "看片", "直播", "剪辑"),
    "game": ("游戏", "手游", "端游", "好玩", "下载", "开服", "更新公告", "版本"),
    "fact": ("冷知识", "知识", "科普", "为什么", "是什么", "由来", "典故", "小知识"),
}

_GREETINGS = ("你好", "在吗", "hello", "hi", "早上好", "晚上好", "早安", "晚安", "嗨")
_THANKS = ("谢谢", "thanks", "感谢", "辛苦", "多谢")
_HELP = ("能做什么", "会什么", "怎么用", "帮助", "help", "功能")

_CONTEXT_HINTS = ("这张图", "这幅图", "这个图", "这张", "这个角色", "看图", "这张画")


def _detect_context(message: str, context: dict[str, Any] | None) -> bool:
    if not context:
        return False
    content_id = context.get("content_id")
    if not content_id:
        return False
    return any(hint in message for hint in _CONTEXT_HINTS)


def _score_experts(message: str) -> tuple[str, int]:
    best_expert = "general"
    best_score = 0
    for expert, keywords in EXPERT_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in message)
        if score > best_score:
            best_expert = expert
            best_score = score
    return best_expert, best_score


def route(message: str, context: dict[str, Any] | None = None) -> tuple[str, str]:
    """返回 (expert, intent)。"""
    text = normalize(message)
    lowered = text.lower()

    if _detect_context(text, context):
        return "image", "context_image"

    if any(word in lowered for word in _HELP):
        return "general", "help"
    if any(word in lowered for word in _THANKS):
        return "general", "thanks"
    if any(word in lowered for word in _GREETINGS):
        return "general", "greeting"

    expert, score = _score_experts(text)
    if score == 0:
        return "general", "unknown"

    if expert == "image":
        if any(word in text for word in ("找", "搜", "查")):
            return "image", "search_image"
        return "image", "recommend_image"
    if expert == "bilibili":
        return "bilibili", "bilibili_daily"
    if expert == "game":
        if any(word in text for word in ("今日", "今天", "更新")):
            return "game", "game_today"
        return "game", "game_search"
    return "fact", "fact_today"


def extract_query(message: str) -> str | None:
    """从自然语言里粗提检索词，去掉常见指令词。"""
    text = normalize(message)
    for noise in (
        "帮我", "请", "我想看", "我要看", "给我", "找几张", "找一些", "找找", "找一下", "找",
        "搜索", "搜一下", "搜", "查一下", "查", "几张", "一些", "的图", "图片", "美图", "插画", "壁纸",
        "有没有", "关于", "看看", "看",
    ):
        text = text.replace(noise, " ")
    tokens = [token for token in text.replace("，", " ").replace("。", " ").split() if token]
    if not tokens:
        return None
    return max(tokens, key=len)[:32]
