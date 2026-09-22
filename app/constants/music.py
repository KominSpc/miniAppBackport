"""音乐每日推荐的提示词常量。

和宠物 LLM 一样，提示词 / 模型参数集中放在 ``app/constants/``，换供应商或调风格
只改这里，业务代码不动。
"""

from __future__ import annotations

from typing import Final

SYSTEM_PROMPT: Final[str] = (
    "你是熟悉网易云 / QQ 音乐曲库的歌单编辑。用户会给你一批他最近在听的歌名，"
    "你要据此推荐风格相近、但**不要和这些歌重复**的歌。"
    '只输出一个 JSON 字符串数组，例如 ["歌名 - 歌手", "歌名 - 歌手"]，'
    "不要解释、不要序号、不要多余文字。"
)

TEMPERATURE: Final[float] = 1.0
MAX_TOKENS: Final[int] = 900

# 送给模型的收藏曲名上限：太多既费 token 也没帮助，几十首足够看出口味。
MAX_SEEDS: Final[int] = 40
# 模型最多给几首候选：多要一点，回源搜索时总有些搜不到。
MAX_CANDIDATES: Final[int] = 60
# 回源搜索的次数上限：每次都要打上游，别为了凑满 30 首无限搜下去。
MAX_LOOKUPS: Final[int] = 36
# 回源阶段的总时限（秒）：上游慢的时候不能把这一页请求一起拖死，到点就收手，
# 剩下的位置用热歌榜补齐。
LOOKUP_BUDGET_SECONDS: Final[float] = 8.0
# LLM 那一轮的硬性时限（秒）。上游卡住时线程会被丢下不管，接口按「模型不可用」回落。
LLM_TIMEOUT_SECONDS: Final[float] = 20.0
# 收藏太少时不必问模型（没有口味样本，问了也是瞎猜），直接用热歌榜。
MIN_SEEDS: Final[int] = 3


def build_prompt(seeds: list[str], limit: int) -> str:
    """把收藏曲名拼成一轮用户消息。"""
    return (
        f"我最近在听这些：{'、'.join(seeds)}。\n"
        f"请推荐 {limit} 首风格接近的歌（可以是同歌手的其他作品），"
        f"严格按 {limit} 条输出 JSON 数组。"
    )
