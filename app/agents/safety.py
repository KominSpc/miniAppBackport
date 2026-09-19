"""对话安全过滤。

规则聚焦「不该聊的话题」与「越界请求」，命中后由 ChatService 返回安全回复，
不向用户复述命中的词，也不回传规则细节。
"""

from __future__ import annotations

from app.config import MAX_CHAT_MESSAGE_LENGTH

MAX_MESSAGE_LENGTH = MAX_CHAT_MESSAGE_LENGTH

_BLOCKED_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "adult",
        ("r18", "r-18", "18+", "nsfw", "色情", "露骨", "裸体", "无码", "里番", "成人内容"),
    ),
    (
        "illegal",
        ("毒品", "赌博", "外挂", "破解版", "盗版下载", "私服", "诈骗", "洗钱"),
    ),
    (
        "danger",
        ("自杀", "自残", "轻生"),
    ),
    (
        "politics",
        ("政治敏感", "颠覆", "暴恐"),
    ),
)

_SAFE_REPLY = "这个话题我不太方便聊，我们看点别的吧～要不要我给你找几张图？"


def detect_blocked_topic(message: str) -> str | None:
    """返回命中的类别，未命中返回 None。"""
    lowered = message.lower()
    for category, keywords in _BLOCKED_TOPICS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return None


def safe_reply() -> str:
    return _SAFE_REPLY


def normalize(message: str) -> str:
    return " ".join(message.split())


def is_empty(message: str) -> bool:
    return not normalize(message)

