"""宠物 LLM 编排：人设 / 角色细节 / 隐藏好感度注入、回落与 TTS 预留路由。

事后质检（出戏检查、重复检查）已经按需求去掉，这里相应地只覆盖「拼提示词、
摘好感度标记、上游失败回落」三条路径。

所有上游调用都注入 ``httpx.MockTransport``，不产生真实网络请求。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.constants import llm as constants
from app.core.errors import AppError
from app.services.pet_llm import persona as persona_checks
from app.services.pet_llm import service as pet_llm
from app.services.pet_llm.client import LlmClient

PLAIN_REPLY = "呀，你终于来啦，我一直在等你呢"


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_base_url": "https://llm.test/v1",
        "llm_api_key": "test-key",
        "llm_model": "deepseek-chat",
        "llm_max_retries": 0,
        "llm_retry_initial_seconds": 0.01,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def install_llm(handler, **overrides: Any) -> list[dict[str, Any]]:
    """把假 LLM 装进服务，返回收集到的请求体列表。"""
    calls: list[dict[str, Any]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return handler(request)

    settings = make_settings(**overrides)
    http = httpx.Client(
        transport=httpx.MockTransport(wrapped), base_url="https://llm.test"
    )
    pet_llm.configure(settings, client=LlmClient(settings, client=http))
    return calls


def reply_once(text: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": text}}]}
        )

    return handler


def reply_sequence(*texts: str):
    pending = list(texts)

    def handler(request: httpx.Request) -> httpx.Response:
        text = pending.pop(0) if pending else texts[-1]
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": text}}]}
        )

    return handler


@pytest.fixture(autouse=True)
def restore_llm():
    """用例结束后恢复成进程启动时的配置，避免污染其它用例。"""
    yield
    pet_llm.reset()
    pet_llm.configure(load_settings())


# --------------------------------------------------------------- 人设解析


def test_resolve_falls_back_to_neutral_persona() -> None:
    # 不预设性格：没带人设时给一个中性壳子，让模型按本性说话
    resolved = persona_checks.resolve(None)

    assert resolved["id"] == constants.DEFAULT_PERSONA["id"]
    assert resolved["name"] == constants.DEFAULT_PERSONA["name"]
    assert resolved["prompt"] == ""
    assert resolved["temperature"] == constants.DEFAULT_TEMPERATURE


def test_resolve_treats_half_filled_persona_as_empty() -> None:
    # 只填名字没写设定：与其发一个空人设给模型，不如回落中性壳子
    resolved = persona_checks.resolve({"id": "x", "name": "只有名字", "prompt": "  "})

    assert resolved["id"] == constants.DEFAULT_PERSONA["id"]


def test_resolve_clamps_and_truncates() -> None:
    resolved = persona_checks.resolve(
        {
            "id": "  custom  ",
            "name": "名" * 80,
            "prompt": "设" * 2000,
            "catchphrase": "口" * 200,
            "scene": "景" * (constants.MAX_PERSONA_SCENE_LENGTH + 500),
            "temperature": 9.5,
        }
    )

    assert resolved["id"] == "custom"
    assert len(resolved["name"]) == constants.MAX_PERSONA_NAME_LENGTH
    assert len(resolved["prompt"]) == constants.MAX_PERSONA_PROMPT_LENGTH
    assert len(resolved["catchphrase"]) == constants.MAX_PERSONA_CATCHPHRASE_LENGTH
    assert len(resolved["scene"]) == constants.MAX_PERSONA_SCENE_LENGTH
    assert resolved["temperature"] == 2.0


def test_resolve_keeps_scene_and_defaults_it_to_none() -> None:
    with_scene = persona_checks.resolve(
        {"id": "p", "name": "傲娇猫", "prompt": "嘴上不饶人", "scene": " 她被人夸时先瞪一眼  "}
    )
    without = persona_checks.resolve({"id": "p", "name": "傲娇猫", "prompt": "嘴上不饶人"})

    assert with_scene["scene"] == "她被人夸时先瞪一眼"
    assert without["scene"] is None


def test_system_prompt_carries_persona_touch_and_facts() -> None:
    resolved = persona_checks.resolve(
        {"id": "p", "name": "傲娇猫", "prompt": "嘴上不饶人", "catchphrase": "才不是呢"}
    )

    prompt = persona_checks.system_prompt(
        resolved, touched_part="左耳", facts={"query": "猫耳"}
    )

    assert constants.BASE_SYSTEM_PROMPT in prompt
    assert "傲娇猫" in prompt
    assert "嘴上不饶人" in prompt
    assert "才不是呢" in prompt
    assert "左耳" in prompt
    assert "query=猫耳" in prompt


def test_system_prompt_carries_scene_and_affection() -> None:
    resolved = persona_checks.resolve(
        {
            "id": "p",
            "name": "傲娇猫",
            "prompt": "嘴上不饶人",
            "scene": "被夸的时候会瞪对方一眼，然后小声嘟囔",
        }
    )

    prompt = persona_checks.system_prompt(resolved, affection=45)

    # 场景片段进提示词，并且要求模型「先读性格再说话」而不是照念
    assert "被夸的时候会瞪对方一眼" in prompt
    assert "社会假面" in prompt
    assert "45/100" in prompt
    assert "亲近" in prompt
    assert "<affection>" in prompt


def test_affection_level_bands() -> None:
    assert persona_checks.affection_level(0)[0] == "陌生"
    assert persona_checks.affection_level(19)[0] == "陌生"
    assert persona_checks.affection_level(20)[0] == "眼熟"
    assert persona_checks.affection_level(45)[0] == "亲近"
    assert persona_checks.affection_level(70)[0] == "交心"
    assert persona_checks.affection_level(100)[0] == "依赖"
    assert persona_checks.affection_level(999)[0] == "依赖"


def test_split_affection_reads_tag_and_strips_it() -> None:
    text, delta = persona_checks.split_affection("今天也陪你一会儿吧\n<affection>+2</affection>")

    assert delta == 2
    assert "affection" not in text
    assert persona_checks.clean(text) == "今天也陪你一会儿吧"


def test_split_affection_reads_the_loose_line_form() -> None:
    text, delta = persona_checks.split_affection("别闹\n好感度: -1")

    assert delta == -1
    assert "好感度" not in text


def test_split_affection_clamps_and_defaults_to_zero() -> None:
    # 模型偶尔写夸张的数字：夹到 ±AFFECTION_DELTA_RANGE
    assert persona_checks.split_affection("呀\n<affection>+99</affection>")[1] == (
        constants.AFFECTION_DELTA_RANGE
    )
    # 没写标记就是 0，正文原样
    text, delta = persona_checks.split_affection(PLAIN_REPLY)
    assert delta == 0
    assert text == PLAIN_REPLY


def test_clean_strips_thinking_and_wrappers() -> None:
    assert persona_checks.clean("  <think>他很烦</think>  「你好呀」  ") == "你好呀"
    assert persona_checks.clean("第一段\n\n第二段") == "第一段"
    assert persona_checks.clean("好   多\n空白") == "好 多 空白"


def test_system_prompt_grants_action_permissions() -> None:
    resolved = persona_checks.resolve(
        {"id": "p", "name": "傲娇猫", "prompt": "嘴上不饶人"}
    )

    prompt = persona_checks.system_prompt(resolved)

    # 宠物要有「真的动一下」的手段：挪位置 / 躲起来 / 重新出现
    assert constants.ACTION_TEMPLATE in prompt
    assert "move:left" in prompt
    assert "hide" in prompt
    assert "show" in prompt


def test_split_actions_keeps_whitelist_and_strips_marker() -> None:
    text, actions = persona_checks.split_actions(
        "我往你那边挪一点<action>move:left</action>"
    )

    assert actions == ["move:left"]
    assert "action" not in text
    assert persona_checks.clean(text) == "我往你那边挪一点"


def test_split_actions_drops_unknown_and_extra_markers() -> None:
    # 白名单之外的取值一律不执行，但标记本身照样摘掉（别把尖括号念给用户听）
    text, actions = persona_checks.split_actions("<action>fly:away</action>")
    assert actions == []
    assert "action" not in text

    # 一次最多 MAX_ACTIONS_PER_REPLY 条：写花了也不会让宠物满屏乱跑
    _, many = persona_checks.split_actions(
        "<action>hide</action> <action>move:right</action>"
    )
    assert len(many) == constants.MAX_ACTIONS_PER_REPLY
    assert many[0] in constants.ALLOWED_ACTIONS

    # 没有标记就是空，正文原样
    plain_text, none = persona_checks.split_actions(PLAIN_REPLY)
    assert none == []
    assert plain_text == PLAIN_REPLY


# --------------------------------------------------------------- 编排


def test_compose_uses_rules_when_llm_disabled() -> None:
    outcome = pet_llm.compose(fallback="规则回复", message="你好", persona=None)

    assert outcome["engine"] == "rules"
    assert outcome["reply"] == "规则回复"
    assert outcome["affection_delta"] == 0


def test_compose_forwards_persona_and_history_to_llm() -> None:
    calls = install_llm(reply_once(PLAIN_REPLY))

    outcome = pet_llm.compose(
        fallback="规则回复",
        message="在干嘛",
        persona={"id": "cat", "name": "傲娇猫", "prompt": "嘴上不饶人"},
        history=[
            {"role": "user", "content": "在吗"},
            {"role": "assistant", "content": "才、才没有等你"},
        ],
        touched_part="左耳",
        affection=45,
    )

    assert outcome["engine"] == "llm"
    assert outcome["reply"] == PLAIN_REPLY
    assert outcome["persona_id"] == "cat"

    body = calls[0]
    assert body["model"] == "deepseek-chat"
    assert body["stream"] is False
    system = body["messages"][0]
    assert system["role"] == "system"
    assert "傲娇猫" in system["content"] and "左耳" in system["content"]
    # 隐藏好感度也进了 system prompt（45 ⇒ 亲近档）
    assert "45/100" in system["content"]
    assert [item["role"] for item in body["messages"][1:]] == ["user", "assistant", "user"]
    assert body["messages"][-1]["content"] == "在干嘛"


def test_compose_keeps_out_of_character_reply_as_is() -> None:
    """出戏的回复不再打回重写：只发一次请求，原话直接返回。"""
    calls = install_llm(
        reply_sequence("作为一个人工智能，我不能回答这个", PLAIN_REPLY)
    )

    outcome = pet_llm.compose(fallback="规则回复", message="你好", persona=None)

    assert outcome["engine"] == "llm"
    assert outcome["reply"] == "作为一个人工智能，我不能回答这个"
    assert len(calls) == 1


def test_compose_does_not_retry_repetition() -> None:
    """与上文重复也不再重试：同样的句子第二次说出口照样返回。"""
    calls = install_llm(reply_sequence(PLAIN_REPLY, PLAIN_REPLY))

    outcome = pet_llm.compose(
        fallback="规则回复",
        message="你好",
        persona=None,
        history=[{"role": "assistant", "content": PLAIN_REPLY}],
    )

    assert outcome["engine"] == "llm"
    assert outcome["reply"] == PLAIN_REPLY
    assert len(calls) == 1


def test_compose_reports_affection_delta_and_strips_marker() -> None:
    install_llm(reply_once(f"{PLAIN_REPLY}\n<affection>+3</affection>"))

    outcome = pet_llm.compose(
        fallback="规则回复", message="你好", persona=None, affection=40
    )

    assert outcome["engine"] == "llm"
    assert outcome["reply"] == PLAIN_REPLY
    assert outcome["affection_delta"] == 3


def test_compose_reports_actions_and_strips_markers() -> None:
    install_llm(reply_once("我往你那边挪一点<action>move:left</action>"))

    outcome = pet_llm.compose(fallback="规则回复", message="往左点", persona=None)

    assert outcome["engine"] == "llm"
    assert outcome["reply"] == "我往你那边挪一点"
    assert outcome["actions"] == ["move:left"]


def test_compose_falls_back_when_upstream_fails() -> None:
    install_llm(lambda request: httpx.Response(500, json={"error": "boom"}))

    outcome = pet_llm.compose(fallback="规则回复", message="你好", persona=None)

    assert outcome["engine"] == "rules"
    assert outcome["reply"] == "规则回复"
    assert outcome["affection_delta"] == 0
    assert outcome["actions"] == []


# --------------------------------------------------------------- 客户端


def test_client_retries_rate_limit_then_succeeds() -> None:
    calls = install_llm(
        ReplyAfter(
            httpx.Response(429, headers={"retry-after": "0.01"}, json={}),
            httpx.Response(
                200, json={"choices": [{"message": {"content": PLAIN_REPLY}}]}
            ),
        ),
        llm_max_retries=1,
    )
    del calls

    assert pet_llm.client().chat([{"role": "user", "content": "hi"}]) == PLAIN_REPLY


def test_client_maps_unauthorized() -> None:
    install_llm(lambda request: httpx.Response(401, json={}))

    with pytest.raises(AppError) as error:
        pet_llm.client().chat([{"role": "user", "content": "hi"}])

    assert error.value.details["reason"] == "llm_unauthorized"


def test_client_rejects_empty_completion() -> None:
    install_llm(lambda request: httpx.Response(200, json={"choices": []}))

    with pytest.raises(AppError) as error:
        pet_llm.client().chat([{"role": "user", "content": "hi"}])

    assert error.value.details["reason"] == "llm_empty_choices"


def test_client_requires_configuration() -> None:
    settings = make_settings(llm_api_key="")
    client = LlmClient(settings)
    assert client.configured is False

    with pytest.raises(AppError) as error:
        client.chat([{"role": "user", "content": "hi"}])

    assert error.value.details["reason"] == "llm_not_configured"


class ReplyAfter:
    """按次序返回固定响应。"""

    def __init__(self, *responses: httpx.Response) -> None:
        self._pending = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return self._pending.pop(0)


# --------------------------------------------------------------- HTTP 接口


def test_chat_reports_rules_engine_when_llm_off(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.post("/v1/pet/chat", json={"message": "你好呀"}, headers=auth)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["engine"] == "rules"
    # 没配 LLM 也如实回报本轮的人设：客户端据此知道设置页选的是哪一份
    assert data["persona_id"] == constants.DEFAULT_PERSONA["id"]
    # 规则引擎不产出好感度标记，分数保持不动
    assert data["affection"] == 0


def test_chat_uses_llm_and_persona(client: TestClient, auth: dict[str, str]) -> None:
    calls = install_llm(reply_once(PLAIN_REPLY))

    response = client.post(
        "/v1/pet/chat",
        json={
            "message": "摸摸",
            "persona": {"id": "cat", "name": "傲娇猫", "prompt": "嘴上不饶人"},
            "touched_part": "左耳",
        },
        headers=auth,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["engine"] == "llm"
    assert data["persona_id"] == "cat"
    assert data["reply"] == PLAIN_REPLY
    assert "傲娇猫" in calls[0]["messages"][0]["content"]


def test_chat_rejects_too_long_persona(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post(
        "/v1/pet/chat",
        json={
            "message": "你好",
            "persona": {"name": "x", "prompt": "设" * (constants.MAX_PERSONA_PROMPT_LENGTH + 1)},
        },
        headers=auth,
    )

    assert response.status_code == 422


def test_tts_route_is_reserved_and_reports_not_configured(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.post("/v1/pet/tts", json={"text": "你好呀"}, headers=auth)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert body["error"]["details"]["reason"] == "tts_not_configured"


def test_tts_requires_text(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post("/v1/pet/tts", json={"text": ""}, headers=auth)

    assert response.status_code == 422
