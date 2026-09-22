"""人设解析、提示词拼装与回复清洗。

这里只做四件事，**不对模型的回复做任何事后质检**（早先版本的「人设检查 + 与上文
重复检查」会把不合格的回复打回重写一次，实际效果是越写越像客服腔）：

- :func:`resolve`：把请求里带的人设归一化；没带 / 半成品都回落到中性人设；
- :func:`system_prompt`：基础规则 + 人设 + 角色细节 + 隐藏好感度 叠成一段 system prompt；
- :func:`clean`：把模型输出收拾成一句能直接显示的话；
- :func:`split_affection`：把回复里那行好感度标记摘出来（累加由调用方做）。

角色细节（:data:`constants.SCENE_TEMPLATE`）是给用户填「某个场景里的对话 / 面对某件事
的反应」用的，提示词里明确要求模型**先读性格再说话**，而不是照着复述片段。
"""

from __future__ import annotations

import re
from typing import Any

from app.constants import llm as constants

_WRAPPER = re.compile(constants.WRAPPER_PATTERN)
_THINKING = re.compile(constants.THINKING_PATTERN, re.IGNORECASE | re.DOTALL)
_AFFECTION_TAG = re.compile(constants.AFFECTION_TAG_PATTERN, re.IGNORECASE)
_AFFECTION_LINE = re.compile(constants.AFFECTION_LINE_PATTERN, re.IGNORECASE)


def _field(source: Any, name: str, default: Any = None) -> Any:
    """同时接受 dict 与 pydantic 模型（接口层传进来的就是后者）。"""
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def clean(raw: str) -> str:
    """把模型输出收拾成一句可以直接显示的话。"""
    text = _THINKING.sub("", raw or "")
    text = text.replace("\r\n", "\n").strip()
    # 只取第一段：模型偶尔会自行追加旁白
    text = text.split("\n\n")[0]
    text = " ".join(text.split())
    return _WRAPPER.sub("", text).strip()


def resolve(persona: Any) -> dict[str, Any]:
    """把请求里带的人设归一化；没带或半成品都回落到中性人设。

    中性人设**不预设任何性格**（prompt 为空）：本机与线上都不再内置人设，用户自己建，
    没建就让模型按本性说话。名字只用来在提示词里称呼它。
    """
    if persona is None:
        return _normalized(dict(constants.DEFAULT_PERSONA))
    name = str(_field(persona, "name", "") or "").strip()
    prompt = str(_field(persona, "prompt", "") or "").strip()
    if not name or not prompt:
        # 只填了名字或只填了设定的半成品，当成没填
        return _normalized(dict(constants.DEFAULT_PERSONA))
    catchphrase = str(_field(persona, "catchphrase", "") or "").strip()
    scene = str(_field(persona, "scene", "") or "").strip()
    return _normalized(
        {
            "id": str(_field(persona, "id", "") or "custom").strip() or "custom",
            "name": name[: constants.MAX_PERSONA_NAME_LENGTH],
            "prompt": prompt[: constants.MAX_PERSONA_PROMPT_LENGTH],
            "catchphrase": catchphrase[: constants.MAX_PERSONA_CATCHPHRASE_LENGTH],
            "scene": scene[: constants.MAX_PERSONA_SCENE_LENGTH],
            "temperature": _field(persona, "temperature", None),
        }
    )


def _normalized(raw: dict[str, Any]) -> dict[str, Any]:
    temperature = raw.get("temperature")
    try:
        value = constants.DEFAULT_TEMPERATURE if temperature is None else float(temperature)
    except (TypeError, ValueError):
        value = constants.DEFAULT_TEMPERATURE
    return {
        "id": raw.get("id") or "custom",
        "name": raw.get("name") or "桌面宠物",
        "prompt": raw.get("prompt") or "",
        "catchphrase": raw.get("catchphrase") or None,
        "scene": raw.get("scene") or None,
        "temperature": min(max(value, 0.0), 2.0),
    }


def affection_level(score: int) -> tuple[str, str]:
    """好感度分数 → (档位名, 这一档允许放开到什么程度)。"""
    value = max(constants.AFFECTION_MIN, min(constants.AFFECTION_MAX, int(score)))
    for upper, name, stance in constants.AFFECTION_LEVELS:
        if value <= upper:
            return name, stance
    last = constants.AFFECTION_LEVELS[-1]
    return last[1], last[2]


def clamp_affection(score: int) -> int:
    return max(constants.AFFECTION_MIN, min(constants.AFFECTION_MAX, int(score)))


def _clamp_delta(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    limit = constants.AFFECTION_DELTA_RANGE
    return max(-limit, min(limit, number))


def split_affection(raw: str) -> tuple[str, int]:
    """把回复里的好感度标记摘出来；返回（干净正文, 本轮增减）。

    先认标签（`<affection>+2</affection>`），认不出再认整行（`好感度: +2`）。
    模型偶尔会把它写进正文中间，摘掉后可能留下一个多余空行，交给 :func:`clean` 处理。
    """
    text = raw or ""
    for pattern in (_AFFECTION_TAG, _AFFECTION_LINE):
        match = pattern.search(text)
        if match:
            delta = _clamp_delta(match.group(1))
            text = f"{text[: match.start()]}\n{text[match.end():]}"
            return text, delta
    return text, 0


def split_actions(raw: str) -> tuple[str, list[str]]:
    """把回复里的动作标记摘出来；返回（干净正文, 允许的动作列表）。

    只认白名单（``constants.ALLOWED_ACTIONS``）里的取值，一次最多
    ``MAX_ACTIONS_PER_REPLY`` 条 —— 模型写花了也不会让宠物在屏幕上乱跑；
    不认识的标记照样从正文里摘掉，免得把尖括号念给用户听。
    """
    text = raw or ""
    pattern = re.compile(constants.ACTION_TAG_PATTERN)
    kept: list[str] = []
    for match in pattern.finditer(text):
        if len(kept) >= constants.MAX_ACTIONS_PER_REPLY:
            break
        action = match.group(1).strip().lower()
        if action in constants.ALLOWED_ACTIONS:
            kept.append(action)
    return pattern.sub("", text), kept


def system_prompt(
    persona: dict[str, Any],
    *,
    touched_part: str | None = None,
    facts: dict[str, Any] | None = None,
    affection: int | None = None,
) -> str:
    """拼 system prompt：基础规则 + 人设（含角色细节）+ 动作权限 + 触碰 + 事实 + 好感度。"""
    catchphrase = persona.get("catchphrase")
    scene = persona.get("scene")
    blocks = [
        constants.BASE_SYSTEM_PROMPT,
        constants.PERSONA_TEMPLATE.format(
            name=persona["name"],
            prompt=persona["prompt"] or "没有额外设定，按你的本性说话。",
            catchphrase_line=(
                constants.CATCHPHRASE_TEMPLATE.format(catchphrase=catchphrase)
                if catchphrase
                else ""
            ),
            scene_line=(constants.SCENE_TEMPLATE.format(scene=scene) if scene else ""),
        ),
        # 动作权限：挪位置 / 躲起来。每轮都给，宠物才有「实际反馈」的手段。
        constants.ACTION_TEMPLATE,
    ]
    if touched_part:
        blocks.append(constants.TOUCH_TEMPLATE.format(part=touched_part))
    if facts:
        readable = "；".join(
            f"{key}={value}" for key, value in facts.items() if value not in (None, "")
        )
        if readable:
            blocks.append(constants.FACT_TEMPLATE.format(facts=readable))
    if affection is not None:
        score = clamp_affection(affection)
        level, stance = affection_level(score)
        blocks.append(
            constants.AFFECTION_TEMPLATE.format(score=score, level=level, stance=stance)
        )
    return "\n\n".join(blocks)
