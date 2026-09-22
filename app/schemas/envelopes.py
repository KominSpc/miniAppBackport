"""契约定义的响应信封。

显式声明为具体子类，导出的 OpenAPI schema 名才能与 contract/openapi.json 完全一致
（FastAPI 对 Envelope[X] 会生成 Envelope_X_ 这类名字）。
"""

from __future__ import annotations

from app.schemas.comic import (
    ComicChapterContent,
    ComicDetail,
    ComicSummary,
    ComicSummaryList,
    ComicTagGroupList,
)
from app.schemas.common import Envelope
from app.schemas.artist import PixivArtist
from app.schemas.content import (
    ContentItem,
    ContentItemList,
    ContentItemPage,
    FavoriteState,
    UgoiraMeta,
)
from app.schemas.daily import DailyFact
from app.schemas.games import GameFilters
from app.schemas.health import DevResetResult, Health
from app.schemas.music import (
    MusicCommentList,
    MusicFavoriteState,
    MusicLyric,
    MusicPlayback,
    MusicPlaylist,
    MusicPlaylistList,
    MusicPlaylistState,
    MusicTrack,
    MusicTrackList,
)
from app.schemas.pet import (
    ConversationPage,
    MessagePage,
    PetChatResponse,
    PetTtsResult,
)
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


class EnvelopePixivArtist(Envelope[PixivArtist]):
    pass


class EnvelopeContentItemList(Envelope[ContentItemList]):
    pass


class EnvelopeContentItemPage(Envelope[ContentItemPage]):
    pass


class EnvelopeFavoriteState(Envelope[FavoriteState]):
    pass


class EnvelopeUgoiraMeta(Envelope[UgoiraMeta]):
    pass


class EnvelopeGameFilters(Envelope[GameFilters]):
    pass


class EnvelopeDailyFact(Envelope[DailyFact]):
    pass


class EnvelopePetTtsResult(Envelope[PetTtsResult]):
    pass


class EnvelopePetChatResponse(Envelope[PetChatResponse]):
    pass


class EnvelopeConversationPage(Envelope[ConversationPage]):
    pass


class EnvelopeMessagePage(Envelope[MessagePage]):
    pass


class EnvelopeComicSummary(Envelope[ComicSummary]):
    pass


class EnvelopeComicSummaryList(Envelope[ComicSummaryList]):
    pass


class EnvelopeComicDetail(Envelope[ComicDetail]):
    pass


class EnvelopeComicChapter(Envelope[ComicChapterContent]):
    pass


class EnvelopeComicTagGroupList(Envelope[ComicTagGroupList]):
    pass


class EnvelopeMusicTrack(Envelope[MusicTrack]):
    pass


class EnvelopeMusicTrackList(Envelope[MusicTrackList]):
    pass


class EnvelopeMusicPlayback(Envelope[MusicPlayback]):
    pass


class EnvelopeMusicLyric(Envelope[MusicLyric]):
    pass


class EnvelopeMusicCommentList(Envelope[MusicCommentList]):
    pass


class EnvelopeMusicPlaylist(Envelope[MusicPlaylist]):
    pass


class EnvelopeMusicPlaylistList(Envelope[MusicPlaylistList]):
    pass


class EnvelopeMusicPlaylistState(Envelope[MusicPlaylistState]):
    pass


class EnvelopeMusicFavoriteState(Envelope[MusicFavoriteState]):
    pass
