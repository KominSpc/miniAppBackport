"""契约定义的 14 个响应信封。

显式声明为具体子类，导出的 OpenAPI schema 名才能与 contract/openapi.json 完全一致
（FastAPI 对 Envelope[X] 会生成 Envelope_X_ 这类名字）。
"""

from __future__ import annotations

from app.schemas.common import Envelope
from app.schemas.content import ContentItem, ContentItemList, ContentItemPage, FavoriteState
from app.schemas.daily import DailyFact
from app.schemas.health import DevResetResult, Health
from app.schemas.pet import ConversationPage, MessagePage, PetChatResponse
from app.schemas.users import AnonymousUser, HistoryDeleteResult, HistoryPage, UserPreferences


class EnvelopeHealth(Envelope[Health]):
    pass


class EnvelopeDevReset(Envelope[DevResetResult]):
    pass


class EnvelopeAnonymousUser(Envelope[AnonymousUser]):
    pass


class EnvelopeUserPreferences(Envelope[UserPreferences]):
    pass


class EnvelopeHistoryPage(Envelope[HistoryPage]):
    pass


class EnvelopeHistoryDelete(Envelope[HistoryDeleteResult]):
    pass


class EnvelopeContentItem(Envelope[ContentItem]):
    pass


class EnvelopeContentItemList(Envelope[ContentItemList]):
    pass


class EnvelopeContentItemPage(Envelope[ContentItemPage]):
    pass


class EnvelopeFavoriteState(Envelope[FavoriteState]):
    pass


class EnvelopeDailyFact(Envelope[DailyFact]):
    pass


class EnvelopePetChatResponse(Envelope[PetChatResponse]):
    pass


class EnvelopeConversationPage(Envelope[ConversationPage]):
    pass


class EnvelopeMessagePage(Envelope[MessagePage]):
    pass
