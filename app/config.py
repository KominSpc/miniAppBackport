"""服务配置与模拟开关。

所有模拟开关都可以由环境变量或请求头控制，`GET /health` 会回显当前生效值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from starlette.requests import Request

MOCK_CONTENT_VERSION = "mock-2026.09"
SERVICE_VERSION = "1.0.0-mock.1"
MAX_CHAT_MESSAGE_LENGTH = 500
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

_TRUTHY = {"1", "true", "yes", "on"}

# pixiv 会按 UA 判定是否为浏览器；用固定桌面 UA，避免默认的 httpx/xxx
DEFAULT_PIXIV_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 音乐上游（Node 服务）按 UA 判定，用一个普通的桌面 UA
DEFAULT_MUSIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 音乐媒体域名白名单：音频直链与封面只允许回源到这些主机，
# 避免 /v1/music/... 变成任意地址的转发器
# 漫画图床白名单：manhuagui 用 hamreus.com / mhgui.com，腾讯用 acimg.cn，
# 动漫屋用 cdndm5.com（子域带编号，因此按域名后缀匹配）
DEFAULT_COMIC_IMAGE_HOSTS: tuple[str, ...] = (
    "hamreus.com",
    "mhgui.com",
    "acimg.cn",
    "cdndm5.com",
)

# 需要补 Referer 的图床：实测 i.hamreus.com 不带 https://www.manhuagui.com/ 必 403，
# 腾讯与动漫屋带上反而可能被挡，因此只登记前两者。
DEFAULT_COMIC_IMAGE_REFERERS: tuple[tuple[str, str], ...] = (
    ("hamreus.com", "https://www.manhuagui.com/"),
    ("mhgui.com", "https://www.manhuagui.com/"),
)

# 漫画可用站点，顺序即搜索的尝试顺序（最稳的在前）
DEFAULT_COMIC_SITES: tuple[str, ...] = ("manhuagui", "qq", "dm5")

DEFAULT_MUSIC_MEDIA_HOSTS: tuple[str, ...] = (
    "music.126.net",
    "music.163.com",
    "stream.qqmusic.qq.com",
    "aqqmusic.tc.qq.com",
    "gtimg.cn",
)

# 模拟开关对应的请求头，便于单个请求临时切换
HEADER_LATENCY = "x-mock-latency-ms"
HEADER_EMPTY = "x-mock-empty"
HEADER_RATE_LIMITED = "x-mock-rate-limited"
HEADER_ERROR = "x-mock-error"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _header_bool(request: Request, name: str) -> bool | None:
    raw = request.headers.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in _TRUTHY


@dataclass(frozen=True)
class Settings:
    service_version: str = SERVICE_VERSION
    content_version: str = MOCK_CONTENT_VERSION
    latency_ms: int = 0
    empty: bool = False
    rate_limited: bool = False
    error: bool = False
    dev_reset_enabled: bool = False
    admin_token: str = "dev-admin-token"
    token_ttl_hours: int = 720
    rate_limit_per_minute: int = 600
    public_base_url: str = ""
    cors_extra_origins: tuple[str, ...] = ()
    # --- 内容源 ---
    # mock：本地夹具（默认）；pixiv：真实上游（需要网络与登录态，见 docs/EXECUTION_PLAN.md 7.x）
    content_source: str = "mock"

    # --- Pixiv 适配器（凭据只在服务端）---
    pixiv_cookie: str = ""
    pixiv_base_url: str = "https://www.pixiv.net"
    pixiv_image_host: str = "https://i.pximg.net"
    pixiv_proxy: str = ""
    pixiv_timeout_seconds: float = 20.0
    pixiv_max_retries: int = 3
    pixiv_retry_initial_seconds: float = 1.5
    pixiv_cache_ttl_seconds: int = 3600
    pixiv_user_agent: str = DEFAULT_PIXIV_USER_AGENT
    pixiv_referer: str = "https://www.pixiv.net/"
    pixiv_accept_language: str = "zh-CN,zh;q=0.9,en;q=0.8"

    # --- 音乐页（上游是仓库同级的 multiPlatformMusicApi Node 服务）---
    # music_source: mock（本地夹具，离线可用）| node（真实上游）
    music_source: str = "mock"
    music_api_base: str = "http://127.0.0.1:19531"
    music_default_platform: str = "netease"
    # 默认请求最高音质；上游未登录时会忽略它并统一返回 128kbps，见 client.py 的说明
    music_default_level: str = "exhigh"
    music_timeout_seconds: float = 15.0
    music_max_retries: int = 2
    music_retry_initial_seconds: float = 1.0
    music_cache_ttl_seconds: int = 1800
    music_media_hosts: tuple[str, ...] = DEFAULT_MUSIC_MEDIA_HOSTS
    music_user_agent: str = DEFAULT_MUSIC_USER_AGENT
    # 音频流地址的签名密钥与有效期：播放器（含 web 的 <audio>）带不上 Authorization，
    # 只能把鉴权挪到 URL 上（见 app/services/music/signature.py）。
    music_stream_secret: str = "miniapp-music-stream"
    music_stream_ttl_seconds: int = 1800
    music_referer: str = "https://music.163.com/"

    # --- 漫画页（上游是仓库同级的 ComicDown 爬虫，由 tools/comic_service.py 包成服务）---
    # comic_source: node（默认，走本机漫画服务）| off（整体关掉，接口返回 503）
    comic_source: str = "node"
    comic_api_base: str = "http://127.0.0.1:19631"
    comic_sites: tuple[str, ...] = DEFAULT_COMIC_SITES
    # 单个站点的取数上限。收紧到 6 秒是刻意的：站点被墙/维护时会**硬挂**到超时
    # （实测 manhuagui 直连时爬虫要 21 秒才报错），而搜索要按顺序试下一个站点、
    # 「最近更新」要等所有站点回来，超时越长用户等得越久（见 source.py 的熔断说明）。
    # 代理可用、站点只是慢的机器上可以调大：COMIC_TIMEOUT_SECONDS=12
    # 爬虫是「抓 HTML 再解析」，不是直接打 API：冷启动那一次实测要 6-7 秒
    # （新建连接 + 搜索页 + 详情页）。原来卡 6 秒，于是第一次搜索必然超时，
    # 熔断再把站点摘掉三分钟，表现就是「漫画搜索时好时坏」。
    comic_timeout_seconds: float = 20.0
    comic_max_retries: int = 1
    comic_retry_initial_seconds: float = 0.8
    comic_cache_ttl_seconds: int = 600
    # 站点熔断时长：某个站点失败后，这段时间内直接跳过它（不再每次白等一个超时）。
    comic_site_cooldown_seconds: int = 180
    comic_image_hosts: tuple[str, ...] = DEFAULT_COMIC_IMAGE_HOSTS
    comic_image_referers: tuple[tuple[str, str], ...] = DEFAULT_COMIC_IMAGE_REFERERS
    comic_user_agent: str = DEFAULT_MUSIC_USER_AGENT
    # 图片回源用的代理（留空=直连）。i.hamreus.com 在国内直连会被重置，本机调试要指向 Clash。
    comic_proxy: str = ""

    # --- 宠物对话的 LLM（OpenAI 兼容；默认节点是 DeepSeek）---
    # 没填 LLM_API_KEY 时视为未启用，宠物对话自动回落到规则引擎。
    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 1
    llm_retry_initial_seconds: float = 1.0

    @property
    def pixiv_enabled(self) -> bool:
        """内容源是否为真实 Pixiv。"""
        return self.content_source.strip().lower() == "pixiv"

    @property
    def llm_enabled(self) -> bool:
        """LLM 是否可用：base_url 与 api_key 都配了才算。"""
        return bool(self.llm_base_url.strip() and self.llm_api_key.strip())

    @property
    def comic_enabled(self) -> bool:
        """漫画源是否启用（走本机漫画服务）。"""
        return self.comic_source.strip().lower() == "node"

    @property
    def music_enabled(self) -> bool:
        """音乐源是否为真实上游（Node 服务）。"""
        return self.music_source.strip().lower() == "node"

    @property
    def page_default(self) -> int:
        return DEFAULT_PAGE_SIZE

    @property
    def page_max(self) -> int:
        return MAX_PAGE_SIZE


@dataclass(frozen=True)
class MockFlags:
    latency_ms: int = 0
    empty: bool = False
    rate_limited: bool = False
    error: bool = False
    dev_reset_enabled: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "latency_ms": self.latency_ms,
            "empty": self.empty,
            "rate_limited": self.rate_limited,
            "error": self.error,
            "dev_reset_enabled": self.dev_reset_enabled,
        }


def load_settings() -> Settings:
    extra = tuple(
        origin.strip()
        for origin in os.getenv("CORS_EXTRA_ORIGINS", "").split(",")
        if origin.strip()
    )
    return Settings(
        latency_ms=_env_int("MOCK_LATENCY_MS", 0),
        empty=_env_bool("MOCK_EMPTY"),
        rate_limited=_env_bool("MOCK_RATE_LIMITED"),
        error=_env_bool("MOCK_ERROR"),
        dev_reset_enabled=_env_bool("DEV_RESET_ENABLED"),
        admin_token=os.getenv("DEV_ADMIN_TOKEN", "dev-admin-token"),
        token_ttl_hours=_env_int("TOKEN_TTL_HOURS", 720),
        rate_limit_per_minute=_env_int("RATE_LIMIT_PER_MINUTE", 600),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        cors_extra_origins=extra,
        content_source=os.getenv("CONTENT_SOURCE", "mock"),
        pixiv_cookie=os.getenv("PIXIV_COOKIE", ""),
        pixiv_base_url=os.getenv("PIXIV_BASE_URL", "https://www.pixiv.net").rstrip("/"),
        pixiv_image_host=os.getenv("PIXIV_IMAGE_HOST", "https://i.pximg.net").rstrip("/"),
        pixiv_proxy=os.getenv("PIXIV_PROXY", ""),
        pixiv_timeout_seconds=_env_float("PIXIV_TIMEOUT_SECONDS", 20.0),
        pixiv_max_retries=_env_int("PIXIV_MAX_RETRIES", 3),
        pixiv_retry_initial_seconds=_env_float("PIXIV_RETRY_INITIAL_SECONDS", 1.5),
        pixiv_cache_ttl_seconds=_env_int("PIXIV_CACHE_TTL_SECONDS", 3600),
        pixiv_user_agent=os.getenv("PIXIV_USER_AGENT", DEFAULT_PIXIV_USER_AGENT),
        comic_source=os.getenv("COMIC_SOURCE", "node"),
        comic_api_base=os.getenv("COMIC_API_BASE", "http://127.0.0.1:19631").rstrip("/"),
        comic_sites=tuple(
            site.strip()
            for site in os.getenv("COMIC_SITES", ",".join(DEFAULT_COMIC_SITES)).split(",")
            if site.strip()
        ),
        comic_timeout_seconds=_env_float("COMIC_TIMEOUT_SECONDS", 20.0),
        comic_max_retries=_env_int("COMIC_MAX_RETRIES", 1),
        comic_retry_initial_seconds=_env_float("COMIC_RETRY_INITIAL_SECONDS", 0.8),
        comic_cache_ttl_seconds=_env_int("COMIC_CACHE_TTL_SECONDS", 600),
        comic_site_cooldown_seconds=_env_int("COMIC_SITE_COOLDOWN_SECONDS", 180),
        comic_image_hosts=tuple(
            host.strip()
            for host in os.getenv(
                "COMIC_IMAGE_HOSTS", ",".join(DEFAULT_COMIC_IMAGE_HOSTS)
            ).split(",")
            if host.strip()
        ),
        comic_image_referers=tuple(
            (pair.split("=", 1)[0].strip(), pair.split("=", 1)[1].strip())
            for pair in os.getenv(
                "COMIC_IMAGE_REFERERS",
                ",".join(f"{host}={referer}" for host, referer in DEFAULT_COMIC_IMAGE_REFERERS),
            ).split(",")
            if "=" in pair
        ),
        comic_user_agent=os.getenv("COMIC_USER_AGENT", DEFAULT_MUSIC_USER_AGENT),
        comic_proxy=os.getenv("COMIC_PROXY", "").strip(),
        music_source=os.getenv("MUSIC_SOURCE", "mock"),
        music_api_base=os.getenv("MUSIC_API_BASE", "http://127.0.0.1:19531").rstrip("/"),
        music_default_platform=os.getenv("MUSIC_DEFAULT_PLATFORM", "netease"),
        music_default_level=os.getenv("MUSIC_DEFAULT_LEVEL", "exhigh"),
        music_timeout_seconds=_env_float("MUSIC_TIMEOUT_SECONDS", 15.0),
        music_max_retries=_env_int("MUSIC_MAX_RETRIES", 2),
        music_retry_initial_seconds=_env_float("MUSIC_RETRY_INITIAL_SECONDS", 1.0),
        music_cache_ttl_seconds=_env_int("MUSIC_CACHE_TTL_SECONDS", 1800),
        music_media_hosts=tuple(
            host.strip().rstrip("/")
            for host in os.getenv(
                "MUSIC_MEDIA_HOSTS", ",".join(DEFAULT_MUSIC_MEDIA_HOSTS)
            ).split(",")
            if host.strip()
        ),
        music_user_agent=os.getenv("MUSIC_USER_AGENT", DEFAULT_MUSIC_USER_AGENT),
        music_stream_secret=os.getenv("MUSIC_STREAM_SECRET", "miniapp-music-stream"),
        music_stream_ttl_seconds=_env_int("MUSIC_STREAM_TTL_SECONDS", 1800),
        llm_provider=os.getenv("LLM_PROVIDER", "deepseek"),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "deepseek-chat"),
        llm_timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 30.0),
        llm_max_retries=_env_int("LLM_MAX_RETRIES", 1),
        llm_retry_initial_seconds=_env_float("LLM_RETRY_INITIAL_SECONDS", 1.0),
    )


def mock_flags_for(request: Request, settings: Settings) -> MockFlags:
    """环境变量为底，请求头可按单次请求覆盖。"""
    flags = MockFlags(
        latency_ms=settings.latency_ms,
        empty=settings.empty,
        rate_limited=settings.rate_limited,
        error=settings.error,
        dev_reset_enabled=settings.dev_reset_enabled,
    )
    raw_latency = request.headers.get(HEADER_LATENCY)
    if raw_latency is not None:
        try:
            flags = replace(flags, latency_ms=max(0, int(raw_latency.strip())))
        except ValueError:
            pass
    for header, field in (
        (HEADER_EMPTY, "empty"),
        (HEADER_RATE_LIMITED, "rate_limited"),
        (HEADER_ERROR, "error"),
    ):
        override = _header_bool(request, header)
        if override is not None:
            flags = replace(flags, **{field: override})
    return flags


def base_url_for(request: Request, settings: Settings) -> str:
    """内容链接的根地址
    默认取自当前请求，使本机 Edge 调试与局域网真机联调都能拿到可达地址；
    需要固定地址时用 PUBLIC_BASE_URL 覆盖。
    """
    if settings.public_base_url:
        return settings.public_base_url
    return str(request.base_url).rstrip("/")
