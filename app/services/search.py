"""检索匹配：中文分词、拼音（全拼 / 首字母）、别名。

模拟服务的数据集固定在几十条，因此这里不搭倒排索引，只做四步处理：

1. **规范化**：NFKC 折叠、转小写、去掉空白与标点，让带空格、全角符号或大小写
   差异的输入与夹具里的写法对齐。
2. **分词**：查询按空白与标点切成多个检索词，要求每个词各自命中（分词后 AND），
   避免「樱花 少女」这类多词查询退化成整串子串匹配。
3. **别名展开**：别名表把「初音未来」「miku」与数据里的标签「初音ミク」串成一组，
   查询侧与数据侧同时展开，中日英写法可以互查。
4. **拼音匹配**：汉字用 pypinyin 生成全拼与首字母。全拼按前缀匹配（「yinghua」命中
   「樱花下的少女」）；首字母只在查询本身是拉丁字母时启用，且要求整词相等或前缀
   相等（「yhxd」命中「樱花下的少女」）。

首字母的两种收紧是必要的：若允许「首字母作为子串」，`sn`（少女 / 少年）与 `yc`
（原创 / 和果子与茶 的尾部）这类缩写会大量误命中。全拼子串也要求至少 3 个字符，
避免 `yc` 这类两字母缩写乱匹配。

匹配全部在内存中完成，不访问外网；拼音只影响召回，不改变返回顺序，游标分页稳定。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping, Sequence

from pypinyin import Style, lazy_pinyin

from app.fixtures.dataset import load_aliases

# 允许作为检索词的字符：数字、拉丁字母、平假名 / 片假名、汉字；其余作为分隔符
_TOKEN_SPLIT = re.compile(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+")

# 单个检索词参与拼音前缀匹配所需的最小长度
_MIN_PINYIN_LENGTH = 4
_MIN_INITIAL_LENGTH = 2


@dataclass(frozen=True)
class FieldIndex:
    """一条数据的可检索形态。"""

    texts: frozenset[str]
    pinyin_full: frozenset[str]
    pinyin_initials: frozenset[str]


def normalize(text: str) -> str:
    """NFKC 折叠 + 小写 + 去空白与标点。"""
    folded = unicodedata.normalize("NFKC", text or "").lower()
    return _TOKEN_SPLIT.sub("", folded)


def _keep_as_is(chunk: str) -> list[str]:
    """pypinyin 的 errors 回调：非汉字原样保留。"""
    return [chunk]


def _pinyin_of(text: str) -> tuple[str, str]:
    """返回（全拼, 首字母）；非汉字部分原样保留。"""
    full = "".join(lazy_pinyin(text, errors=_keep_as_is))
    initials = "".join(lazy_pinyin(text, style=Style.FIRST_LETTER, errors=_keep_as_is))
    return normalize(full), normalize(initials)


@lru_cache(maxsize=1)
def _alias_index() -> Mapping[str, frozenset[str]]:
    """别名反向索引：任一写法 -> 同组的全部写法。"""
    index: dict[str, set[str]] = {}
    for canonical, aliases in load_aliases().items():
        group = {normalize(canonical), *(normalize(alias) for alias in aliases)} - {""}
        for member in group:
            index.setdefault(member, set()).update(group)
    return {key: frozenset(value) for key, value in index.items()}


@lru_cache(maxsize=8192)
def literal_forms(text: str) -> frozenset[str]:
    """一个词的字面写法：本身 + 同组别名。"""
    base = normalize(text)
    if not base:
        return frozenset()
    return frozenset({base} | set(_alias_index().get(base, frozenset())))


def tokens(query: str) -> list[str]:
    """把查询切成检索词：空白、标点、全角符号都作为分隔符。"""
    folded = unicodedata.normalize("NFKC", query or "").lower()
    return [part for part in _TOKEN_SPLIT.split(folded) if part]


def index_forms(*fields: str) -> FieldIndex:
    """把一条数据的所有可检索字段折叠成 FieldIndex。"""
    texts: set[str] = set()
    full: set[str] = set()
    initials: set[str] = set()
    for field in fields:
        if not field:
            continue
        forms = literal_forms(field)
        texts |= forms
        for form in forms:
            form_full, form_initials = _pinyin_of(form)
            full.add(form_full)
            initials.add(form_initials)
    return FieldIndex(
        texts=frozenset(texts),
        pinyin_full=frozenset(value for value in full if value),
        pinyin_initials=frozenset(value for value in initials if value),
    )


def _has_cjk(text: str) -> bool:
    return any("\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff" for char in text)


def _token_hits(index: FieldIndex, token: str) -> bool:
    """单个检索词是否命中。"""
    forms = literal_forms(token)
    if not forms:
        return False
    # 1) 字面匹配：任意写法作为子串出现在任意字段里
    if any(form in text for form in forms for text in index.texts):
        return True
    # 2) 全拼匹配：写法本身或其全拼是字段全拼的前缀
    for form in forms:
        candidates = {form, _pinyin_of(form)[0]} - {""}
        for candidate in candidates:
            if len(candidate) < _MIN_PINYIN_LENGTH:
                continue
            if any(field.startswith(candidate) for field in index.pinyin_full):
                return True
    # 3) 首字母匹配：仅当查询本身是拉丁字母（用户直接敲缩写）
    if _has_cjk(token):
        return False
    if len(token) < _MIN_INITIAL_LENGTH:
        return False
    return any(
        field == token or field.startswith(token) for field in index.pinyin_initials
    )


def match_count(index: FieldIndex, query_tokens: Sequence[str]) -> int:
    """命中的检索词数量。"""
    return sum(1 for token in query_tokens if _token_hits(index, token))


def matches(index: FieldIndex, query_tokens: Sequence[str]) -> bool:
    """全部检索词都命中才算匹配（分词后 AND）。"""
    if not query_tokens:
        return False
    return match_count(index, query_tokens) == len(query_tokens)