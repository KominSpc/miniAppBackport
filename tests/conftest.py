"""测试夹具：每个用例前后清空内存仓储与限流窗口。"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.ratelimit import limiter
from app.core.timeutil import now
from app.main import app
from app.repositories.memory import store
from app.services.comic import source as comic_source
from app.services.music import source as music_source

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_state():
    store.reset()
    limiter.clear()
    # 图片字节缓存是进程级的：不清掉的话，后一个用例用同一张图但换了假传输时
    # 会读到前一个用例缓存下来的字节，断言莫名其妙地失败。
    comic_source.reset()
    music_source.reset()
    yield
    store.reset()
    limiter.clear()
    comic_source.reset()
    music_source.reset()


@pytest.fixture
def anonymous(client: TestClient) -> dict:
    response = client.post("/v1/users/anonymous", json={"platform": "web", "app_version": "test"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.fixture
def auth(anonymous: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {anonymous['access_token']}"}


@pytest.fixture
def second_user(client: TestClient) -> dict[str, str]:
    response = client.post("/v1/users/anonymous", json={"platform": "android", "app_version": "test"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.fixture
def expire_token():
    """把某个令牌改成已过期，用于验证 TOKEN_EXPIRED 分支。"""

    def _expire(token: str) -> None:
        record = store.resolve_token(token)
        assert record is not None
        record["expires_at"] = now() - timedelta(hours=1)

    return _expire

