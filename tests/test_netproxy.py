"""代理选择：显式配置 > 系统代理 > 直连；回环永远直连。

现场教训（2026-09-21）：代理地址写死在本机配置里，用户换网络（或干脆关掉代理）
之后服务端还在往一个没人监听的端口发请求，表现成「接口时好时坏」。这里守住
「不写死、跟着操作系统走」这条线。
"""

from __future__ import annotations

import pytest

from app.core import netproxy


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    """每个用例前后都丢掉缓存，避免上一个用例的探测结果串味。"""
    netproxy.system_proxy(refresh=True)
    yield
    netproxy.system_proxy(refresh=True)


def use_proxies(monkeypatch: pytest.MonkeyPatch, proxies: dict[str, str] | object) -> None:
    """替换探测结果并立刻刷新缓存。"""
    monkeypatch.setattr(netproxy, "getproxies", proxies)
    netproxy.system_proxy(refresh=True)


def test_loopback_never_goes_through_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    use_proxies(monkeypatch, lambda: {"https": "http://127.0.0.1:7897"})
    assert netproxy.proxy_for("http://127.0.0.1:19531/search") is None
    assert netproxy.proxy_for("http://localhost:18421/v1/music/daily") is None
    assert netproxy.proxy_for("http://127.0.0.5:8080/") is None


def test_explicit_proxy_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    use_proxies(monkeypatch, lambda: {"https": "http://127.0.0.1:7897"})
    assert (
        netproxy.proxy_for("https://www.pixiv.net/ajax/illust/1", "http://10.0.0.1:1080")
        == "http://10.0.0.1:1080"
    )


def test_falls_back_to_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    use_proxies(monkeypatch, lambda: {"https": "http://127.0.0.1:7897"})
    assert netproxy.proxy_for("https://i.pximg.net/x.jpg") == "http://127.0.0.1:7897"


def test_direct_when_no_proxy_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    use_proxies(monkeypatch, lambda: {"http": "", "https": ""})
    assert netproxy.proxy_for("https://i.pximg.net/x.jpg") is None


def test_probe_failure_means_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict[str, str]:
        raise OSError("no registry")

    use_proxies(monkeypatch, boom)
    assert netproxy.proxy_for("https://i.pximg.net/x.jpg") is None


def test_is_local_host() -> None:
    assert netproxy.is_local_host("127.0.0.1")
    assert netproxy.is_local_host("127.0.0.9")
    assert netproxy.is_local_host("LocalHost")
    assert netproxy.is_local_host("[::1]")
    assert not netproxy.is_local_host("music.163.com")
