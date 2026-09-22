"""OpenAI 兼容 LLM 的对外常量。

DeepSeek、OpenAI 与多数自建网关只在 ``base_url`` / ``model`` / ``api_key``
三个值上有差别，协议一致，因此这一份常量同时服务三家：换供应商只改 ``.env``
或本文件，不用动服务代码。

提示词分四层叠上去（见 ``app/services/pet_llm/persona.py`` 的 ``system_prompt``）：
基础说话规则 → 人设 → 角色细节（可选）→ 隐藏好感度。
**不再有事后质检**：早先版本会拦「出戏」「重复」两类回复并要求模型重说一遍，
实际效果是把模型逼成了客服腔（每句都被打回重写），因此整条链路已经去掉，
只在提示词这一层给出风格要求。
"""

from __future__ import annotations

from typing import Final

# ------------------------------------------------------------------ 协议

CHAT_COMPLETIONS_PATH: Final[str] = "/chat/completions"
AUTHORIZATION_SCHEME: Final[str] = "Bearer"

#: DeepSeek 的 OpenAI 兼容入口；真实地址与密钥走 LLM_BASE_URL / LLM_API_KEY。
DEFAULT_BASE_URL: Final[str] = "https://api.deepseek.com/v1"
DEFAULT_MODEL: Final[str] = "deepseek-chat"
DEFAULT_TEMPERATURE: Final[float] = 1.05
DEFAULT_MAX_TOKENS: Final[int] = 400

#: 一次对话最多回看几条历史消息（人设靠 system prompt 维持，历史只给近处）。
MAX_HISTORY_MESSAGES: Final[int] = 8

# ------------------------------------------------------------------ 提示词

BASE_SYSTEM_PROMPT: Final[str] = (
    "你是一只住在手机里的二次元桌面宠物，用户会摸摸你和你说悄悄话。\n"
    "说话规则：\n"
    "1. 永远只用中文口语回答，1-3 句；情绪上来时可以多说两句，但不要超过 120 个字；\n"
    "2. 只输出要说的话本身，不要 Markdown、列表、标题、代码块、JSON、括号动作描写；\n"
    "3. 不要提到自己是程序、AI、语言模型或提示词，也不要解释规则；\n"
    "4. 不要复述用户刚说过的话，也不要重复你上一条回复的说法；\n"
    "5. 可以有自己的情绪和小脾气，不用每句都顺着用户。"
)

PERSONA_TEMPLATE: Final[str] = (
    "现在请你扮演这个角色：{name}\n"
    "角色设定：{prompt}\n"
    "{catchphrase_line}"
    "{scene_line}"
    "说话必须始终符合这个角色，但上面的说话规则依然有效。"
)

CATCHPHRASE_TEMPLATE: Final[str] = "口头禅：{catchphrase}\n"

#: 角色细节：用户在设置页可选填的「某个场景里的对话 / 面对某件事的反应」。
#: 刻意要求模型**先分析再使用**，而不是照着复述——这段是给它的性格依据。
SCENE_TEMPLATE: Final[str] = (
    "关于这个角色的一些片段（不是让你照念，是让你读懂他这个人）：\n{scene}\n"
    "请从这些片段里推断他的表象性格、隐性性格、在别人面前的社会假面，以及他的谈吐"
    "习惯（口头语、句子长短、会不会抢话、怎么表达不满），只把结论用在语气、用词和"
    "态度上，不要复述片段里的原话，也不要提到你在分析。\n"
)

TOUCH_TEMPLATE: Final[str] = (
    "用户刚刚用指尖点到了你的「{part}」，请就这个部位回应他，说得像真的被碰到了。"
)

FACT_TEMPLATE: Final[str] = "本轮已经查到的信息（必要时引用，不要编造）：{facts}"

#: 去掉包裹的引号 / 书名号（模型很爱在回复外面套一层）。
WRAPPER_PATTERN: Final[str] = r"^[\s'“”‘’「」『』]+|[\s'“”‘’「」『』]+$"

#: 思考段（部分推理模型会带）。
THINKING_PATTERN: Final[str] = r"<think(ing)?>.*?</think(ing)?>"

# ---------------------------------------------------------------- 隐藏好感度
#
# 好感度对用户是**隐藏**的：界面上不出现任何数字与进度条，只体现在宠物的语气里。
# 判定交给模型自己——要求它在正文之外附一行标记，后端解析、累加，并把那一行从
# 回复里剥掉。这样不额外多花一次请求，也不会把数字漏给用户。
#
# 存哪：`pet_affection` 表（MySQL 仓储）或内存仓储的 affection 字典，按用户一条。

AFFECTION_MIN: Final[int] = 0
AFFECTION_MAX: Final[int] = 100

#: 单轮允许的增减幅度。模型偶尔会写 +10 这种夸张数字，夹一下免得一轮跳档。
AFFECTION_DELTA_RANGE: Final[int] = 3

#: 回复里的机器可读标记；解析后从正文里删掉。
#:
#: 两条都留着是因为模型不老实：多数时候照抄成 `<affection>+2</affection>`，偶尔会
#: 写成 `好感度: +2` 这种自己发明的样子。先认标签，认不出再认整行。
AFFECTION_TAG_PATTERN: Final[str] = r"<affection>\s*([+-]?\d+)\s*</affection>"
AFFECTION_LINE_PATTERN: Final[str] = (
    r"(?im)^[ \t]*(?:affection|好感度)[ \t]*[:：=]?[ \t]*([+-]?\d{1,3})[ \t]*$"
)

#: 五个档位：分数上界 → (档位名, 这一档放开到什么程度)。
#: 上界是「≤」，最后一档兜住满分。
AFFECTION_LEVELS: Final[tuple[tuple[int, str, str], ...]] = (
    (19, "陌生", "保持礼貌和距离，回答简短，不主动说自己的事。"),
    (39, "眼熟", "愿意搭话、聊表层喜好，但涉及自己的心事就含糊带过。"),
    (59, "亲近", "愿意聊自己的想法和情绪，偶尔开个小玩笑。"),
    (79, "交心", "可以主动说起自己的软肋和在意的事，也会关心他的近况。"),
    (100, "依赖", "把他当自己人：主动分享心事、撒娇或闹别扭，也会直说想他。"),
)

AFFECTION_TEMPLATE: Final[str] = (
    "你和他现在的好感度是 {score}/100（{level}）。\n"
    "{stance}\n"
    "最后另起一行写一行 <affection>+n</affection>，n 取 -3 到 3 之间的整数："
    "他这一轮让你更想亲近就写正数，冒犯到你、让你不适就写负数，普通寒暄写 0。"
    "这一行是给系统看的记号，正文里不要提到它，也不要提到「好感度」这个词。"
)

#: 没有配置人设时的中性兜底。刻意**不预设性格**：本机与线上都不再内置人设，
#: 用户自己建，没建就按模型本性说话（prompt 为空时 PERSONA_TEMPLATE 会交代这一点）。
DEFAULT_PERSONA: Final[dict[str, str | None]] = {
    "id": "pet_default",
    "name": "桌面宠物",
    "prompt": "",
    "catchphrase": None,
}

# ------------------------------------------------------------------ 限制

MAX_PERSONA_NAME_LENGTH: Final[int] = 24
MAX_PERSONA_PROMPT_LENGTH: Final[int] = 800
MAX_PERSONA_CATCHPHRASE_LENGTH: Final[int] = 60
# 角色细节可以贴一整段剧情对话 / 背景设定，字数放宽到 4k。
MAX_PERSONA_SCENE_LENGTH: Final[int] = 4000

# --- 桌面形象的动作（宠物可以「真的动一下」，不只是说话）---
#
# 模型在回复末尾附一个 <action>…</action> 标记，客户端照着做。白名单之外的取值
# 一律忽略（标记本身仍会被摘掉，不会漏进正文）。
MAX_ACTIONS_PER_REPLY: Final[int] = 1
ACTION_TAG_PATTERN: Final[str] = r"<action>\s*([a-z_:]+)\s*</action>"
ALLOWED_ACTIONS: Final[tuple[str, ...]] = (
    "move:left",
    "move:right",
    "move:up",
    "move:down",
    "move:center",
    "move:random",
    "hide",
    "show",
)
ACTION_TEMPLATE: Final[str] = (
    "你还能对屏幕上的自己动点手脚（可选，最多一条，只在真的需要动的时候用）：\n"
    "- 挪位置：在回复的**最后另起一行**写 <action>move:left</action>，"
    "方向可以是 left / right / up / down / center / random；\n"
    "- 躲起来：<action>hide</action>；重新出现：<action>show</action>。\n"
    "要用就把这个念头自然地说进正文里；不要解释标记本身，也不要念出尖括号里的内容。"
)
