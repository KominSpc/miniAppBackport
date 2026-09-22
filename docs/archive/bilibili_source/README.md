# 已下线的 B 站内容源（`VIDEO_SOURCE=bilibili`）快照

2026-09-21：用户要求「下线 b 站冷知识，不涉及推荐页」。实际摘除范围比一条冷知识大 ——
**整条 B 站内容源 + 每日页接口一起下线**：在线代码、接口、契约条目全部摘除，这里另存一份
快照，方便日后独立移植或回滚。前端对应的页面快照见 `mini_app/docs/archive/daily_page/`。

## 快照内容

```
app/api/v1/daily.py                 # /v1/daily/bilibili、/v1/daily/fact
app/services/bilibili/__init__.py
app/services/bilibili/adapter.py    # B 站条目 → 契约 ContentItem
app/services/bilibili/client.py     # 上游出口：GET JSON + GET 字节
app/services/bilibili/source.py     # 装配 + 三级 TTL 缓存 + 限速 + 风控回落
scripts/bilibili_smoke.py           # 只读冒烟（热门 / 分类榜 / 封面）
tests/test_bilibili.py
tests/test_daily.py
```

## 下线时改动过的在线文件（**这些文件没有在快照里，需要手工回滚**）

| 文件 | 改动 |
|---|---|
| `app/main.py` | 去掉 `daily_routes` 的 import 与 `include_router`；去掉 `bilibili_source.configure(...)` 调用 |
| `app/api/v1/system.py` | `/health` 的 `degraded` 只看 `pixiv_source.degraded()` |
| `app/services/catalog.py` | 去掉 bilibili import、`all_videos()` 里的 `bilibili_source.active()` 分支、`item_by_id()` 里的 bilibili 分支（`build_video` 与本地夹具视频**保留**） |
| `app/api/v1/images.py` | `_REMOTE_SOURCES` 只留 `px_`（pixiv）前缀 |
| `app/config.py` | 删 `DEFAULT_BILIBILI_USER_AGENT` 常量、整段 `bilibili_*` 设置、`bilibili_enabled` 属性、对应 env 解析块 |
| `.env` | 删 `VIDEO_SOURCE` / `BILIBILI_PROXY` 两行 |
| `tests/test_mock_switches.py`、`tests/test_payloads.py` | 去掉 B 站相关断言（`test_payloads.py` 改用 `POST /v1/pet/chat {"message":"推荐点视频"}` 取视频条目） |
| `../mini_app/contract/openapi.json` | 删 `/v1/daily/bilibili`、`/v1/daily/fact` 两个路径与 `DailyFact`、`EnvelopeDailyFact` 两个 schema，然后 `scripts/export_openapi.py --sync` |

## 特意保留

- **`ContentType.video` / `PetExpert.bilibili` / `ContentSource.bilibili` 等枚举值**：它们是
  线上协议字段，桌宠的「视频」分类也还在用，删掉会让客户端解析旧数据直接失败；
- **`app/services/catalog.py` 的 `build_video` 与本地夹具视频**：桌宠建议里的视频条目仍可展示；
- **前端 `lib/core/utils/video_launcher.dart`**：桌宠的视频建议仍然要走它，只是从
  `lib/features/daily/` 挪到了 `core/utils/`；
- **推荐页的 `/v1/images/daily`**：那是 pixiv 每日热图，与 B 站无关，未受任何影响。

## 恢复方式

1. 把本目录的 `app/`、`scripts/`、`tests/` 覆盖回 `miniAppBackport` 对应目录；
2. 按上表逐条恢复 `app/main.py`、`app/api/v1/system.py`、`app/services/catalog.py`、
   `app/api/v1/images.py`、`app/config.py`、`.env` 与两个测试文件的改动；
3. 在 `.env` 里重新填 `VIDEO_SOURCE=bilibili`（以及可选的 `BILIBILI_COOKIE` / `BILIBILI_PROXY`），
   重启后端；
4. 前端按 `mini_app/docs/archive/daily_page/README.md` 恢复每日页；
5. 跑 `.venv\Scripts\python.exe -m pytest -q` 与 `scripts\bilibili_smoke.py` 验证。

注意：快照是**死代码**，不参与测试与静态检查，后续改动不会同步过去。
