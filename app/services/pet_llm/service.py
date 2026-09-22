"""宠物对话的 LLM 编排。

一轮对话的顺序：

1. 规则引擎先跑一遍，得到专家意图、建议卡片与**确定性回复**（用作兜底）；
2. LLM 可用时，用「基础规则 + 人设 + 角色细节 + 隐藏好感度 + 本轮事实」拼 system
   prompt，带上最近几条历史发过去；
3. 拿到回复后只做两件机械的事：摘出好感度标记（累加交给上层），再把文本收拾干净；
4. 上游失败就回落到第 1 步的确定性回复 —— 聊天不会因为模型抽风而中断
   （``engine`` 字段如实告诉客户端这句话是谁说的）。

**没有事后质检**：不判断回复是否「出戏」，也不比对是否和上文重复。早先版本会把这些
不合格的回复打回重写一次，代价是模型被逼成客服腔、回复越来越僵（需求里点名要去掉），
风格要求现在只写在提示词里。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from app.config import Settings
from app.constants import llm as constants
from app.core.errors import AppError
from app.services.pet_llm import persona as persona_checks
from app.services.pet_llm.client import LlmClient

logger = logging.getLogger("miniappbackport.pet_llm")

_settings: Settings | None = None
_client: LlmClient | None = None

ENGINE_RULES = "rules"
ENGINE_LLM = "llm"


def configure(settings: Settings, *, client: LlmClient | None = None) -> None:
    """启动时注入配置（``create_app`` 调用）；``client`` 供测试注入假传输。"""
    global _settings, _client
    _settings = settings
    if _client is not None and _client is not client:
        _client.close()
    _client = client or LlmClient(settings)


def reset() -> None:
    """测试用：清空配置与客户端。"""
    global _settings, _client
    if _client is not None:
        _client.close()
    _settings = None
    _client = None


def enabled() -> bool:
    return _client is not None and _client.configured


def client() -> LlmClient:
    if _client is None:
        raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "llm_not_configured"})
    return _client


def _history_messages(history: Iterable[dict[str, Any]] | None) -> list[dict[str, str]]:
    """只取近处若干条，且只保留 user / assistant 两种角色。"""
    messages: list[dict[str, str]] = []
    for item in history or []:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": str(role), "content": content.strip()})
    return messages[-constants.MAX_HISTORY_MESSAGES :]


def build_messages(
    *,
    message: str,
    persona: dict[str, Any],
    history: Iterable[dict[str, Any]] | None = None,
    touched_part: str | None = None,
    facts: dict[str, Any] | None = None,
    affection: int | None = None,
) -> list[dict[str, str]]:
    """system + 历史 + 本轮用户消息。"""
    messages = [
        {
            "role": "system",
            "content": persona_checks.system_prompt(
                persona,
                touched_part=touched_part,
                facts=facts,
                affection=affection,
            ),
        }
    ]
    messages.extend(_history_messages(history))
    messages.append({"role": "user", "content": message})
    return messages


def compose(
    *,
    fallback: str,
    message: str,
    persona: Any,
    history: Iterable[dict[str, Any]] | None = None,
    touched_part: str | None = None,
    facts: dict[str, Any] | None = None,
    affection: int | None = None,
) -> dict[str, Any]:
    """生成一句回复；返回值里带上本轮的好感度增减（``affection_delta``）。"""
    resolved = persona_checks.resolve(persona)
    if not enabled():
        return {
            "reply": fallback,
            "engine": ENGINE_RULES,
            "persona_id": resolved["id"],
            "affection_delta": 0,
            "actions": [],
        }

    messages = build_messages(
        message=message,
        persona=resolved,
        history=history,
        touched_part=touched_part,
        facts=facts,
        affection=affection,
    )
    try:
        raw = client().chat(messages, temperature=resolved["temperature"])
    except AppError as error:
        # 上游不可用不该让聊天失败：记下原因，用规则引擎的回复兜底
        reason = str(error.details.get("reason") or error.code)
        logger.warning("LLM 调用失败，回落到规则引擎：%s", reason)
        return {
            "reply": fallback,
            "engine": ENGINE_RULES,
            "persona_id": resolved["id"],
            "affection_delta": 0,
            "actions": [],
        }

    text, delta = persona_checks.split_affection(raw)
    # 动作标记与好感度走同一条「先摘标记、再收拾正文」的路。
    text, actions = persona_checks.split_actions(text)
    reply = persona_checks.clean(text) or fallback
    return {
        "reply": reply,
        "engine": ENGINE_LLM,
        "persona_id": resolved["id"],
        "affection_delta": delta,
        "actions": actions,
    }
