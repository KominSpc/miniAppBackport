# miniAppBackport

「二次元美图与每日内容应用」的**模拟服务**（契约实现方）。

Flutter 客户端仓库：`KominSpc/MiniApp`，其 `contract/openapi.json` 是客户端-服务端契约的单一事实来源。
本仓库按该契约实现全部 `/v1` 接口，供本地与局域网联调使用；后续接入真实上游（Pixiv / B 站 / 游戏库）时，路径与响应结构保持不变。

## 快速开始

```powershell
pwsh run.ps1 -Init      # 建 venv、装依赖、生成夹具与示例图片
pwsh run.ps1            # 启动 http://127.0.0.1:18421
pwsh run.ps1 -Reload    # 开发模式，自动重载
pwsh run.ps1 -Check     # 只做契约校验
```

或使用容器：

```powershell
docker compose up --build
```

启动后：

- 接口文档：`http://127.0.0.1:18421/docs`
- 健康检查与当前开关：`GET /health`
- 局域网真机联调：服务默认监听 `0.0.0.0`，手机访问 `http://<本机IP>:18421`

## 目录结构

```
app/
  main.py            应用装配：中间件、异常处理、CORS
  config.py          配置与模拟开关（环境变量 + 请求头）
  deps.py            依赖注入：配置、模拟开关、Bearer 鉴权、分页参数
  core/              信封、错误码、分页游标、时区、限流、空数据中间件
  schemas/           契约模型：枚举、Meta/ErrorInfo、ContentItem、各接口模型与 14 个信封
  api/v1/            system / users / images / daily / games / pet / dev 路由
  services/          业务编排：编目、检索匹配、互动、每日、宠物
  agents/            对话安全过滤、专家路由、回复组装
  repositories/      内存仓储 + MySQL 骨架（P1）
  fixtures/          数据集（data/*.json）与示例图片生成
  static/images/     生成的示例图（已 gitignore）
tests/               pytest：契约、边界、隔离、开关、CORS
scripts/             fixture 生成、示例图生成、OpenAPI 导出、契约比对
```

## 关键约定

| 项目 | 约定 |
|---|---|
| 基础路径 | `/v1`，健康检查为 `/health` |
| 信封 | 所有响应均为 `{data, meta, error}`，无例外 |
| 时区 | 固定 `Asia/Shanghai`（+08:00） |
| 分页 | 不透明游标，默认 20 / 最大 50；非法游标 400，越界 422 |
| 鉴权 | `Authorization: Bearer <token>`；缺失/非法 401 `UNAUTHORIZED`，过期 401 `TOKEN_EXPIRED` |
| 收藏 | 幂等 `PUT` / `DELETE`，重复调用结果一致 |
| 敏感内容 | 服务端按 `safe_mode` 强制过滤，`is_sensitive` 不下发到过滤后的列表 |
| 图片 | `cover_url` 等一律指向本服务，客户端不接触第三方图床与凭据 |

### 检索匹配规则

`GET /v1/images/search?q=` 与 `?tag=` 共用 `app/services/search.py` 的一套匹配逻辑，支持需求文档要求的
「中文分词、拼音、别名」：

| 方式 | 说明 | 例子 |
|---|---|---|
| 字面 | 查询词或其别名作为子串命中标题 / 副标题 / 标签 / 作者 | `初音未来` 命中标签为 `初音ミク` 的作品 |
| 全拼 | 汉字全拼与字段全拼做**前缀**匹配，最少 4 字符 | `yinghua` 命中「樱花下的少女」 |
| 首字母 | 仅当查询本身是拉丁字母时启用，整词相等或前缀匹配，最少 2 字符 | `yhxd` 命中「樱花下的少女」 |
| 分词 | 查询按空白与标点切词，要求**每个词各自命中** | `樱花 少女` 命中，`樱花 便利店` 返回空 |

限制来自实测的误命中：`sn`（少女 / 少年）、`yc`（原创 / 「和果子与茶」的尾部）这类缩写若允许子串匹配会大量
误命中，`ying`（桜 / 「湖中倒影」的 daoying）这类单音节同理，因此全拼只做前缀、首字母只做整词或前缀。

拼音只影响召回，不改变返回顺序，游标分页仍然稳定。别名表在 `scripts/fixture_tables.py` 的 `ALIASES`，
生成到 `app/fixtures/data/aliases.json`；拼音由 `pypinyin` 在运行时计算，不联网。
### 搜索筛选参数（2026-09-19 新增）

`GET /v1/images/search` 支持四个可选筛选（前两项透传上游，后两项是 R18 与动图）：

| 参数 | 取值 | 上游映射 | 说明 |
|---|---|---|---|
| `min_bookmarks` | 0..1000000 | `bl` | 收藏数下限（pixiv 的热度指标，即界面上的「点赞数」筛选） |
| `lang` | `zh` / `ja` / `en` | `lang` | 标签 / 标题翻译语言，缺省 `zh` |
| `r18` | `true` / `false` | `mode=all` | 是否包含 R18；**还需该用户偏好 `safe_mode=false`**，否则强制按 safe 处理 |
| `animated` | `true` / `false` | `type=ugoira` | 只看动图（うごイラ）；上游忽略 `type` 时由服务端按 `illustType == 2` 本地过滤 |

筛选条件会编进分页游标的 scope（`images:search:<词>:<bl>:<lang>:<r18>:<animated>`），换筛选后旧游标返回 400
而不是页码错位；上游页缓存键也带筛选组合，不同筛选不会互相命中。

动图会**多扫几页**再本地过滤：上游忽略 `type` 时返回的是混合结果，而混合结果里动图占比很低
（实测「初音ミク」180 条里只有 2 条），不多扫几页第一页就会是空的。

图片 payload 里的 `illust_type`（0 插画 / 1 漫画 / 2 动图）就是上游 `illustType`，客户端据此在
卡片上打「动图」角标；它是契约字段（`ImagePayload.illust_type`），非 pixiv 源为 null。

> **未登录 pixiv 时上游会忽略 `bl` / `lang` / `mode` / `type`**：实测（本机代理）`bl=1000/5000/10000/100000`、
> `mode=safe/r18/all`、`type=all/ugoira` 的返回完全一致，条目里连 `bookmarkCount` 都没有。要让 R18 /
> 收藏数 / 语言筛选真正生效，需要把浏览器登录后的完整 Cookie 填进 `.env` 的 `PIXIV_COOKIE` 再重启；
> 动图不依赖登录态（本地按 `illustType` 过滤即可）。配好 Cookie 后用下面的诊断命令复核。
### 为什么必须做图片代理

`i.pximg.net` 强校验 `Referer`，缺少或伪造 Referer 的请求一律 403，客户端直连必然失败。
因此 `GET /v1/images/{id}/file?variant=thumb|regular|original&page=N` 是唯一取图通道；
模拟期它输出本地程序化绘制的占位图，接入真实适配器后行为不变。

> Flutter web 上 `Image.network` 无法携带 `Authorization` 头，客户端需用带鉴权的 HTTP 请求取字节后走 `Image.memory`。

## 模拟行为开关

环境变量为默认值，请求头可对单次请求覆盖；`GET /health` 会回显当前生效值。

| 开关 | 环境变量 | 请求头 |
|---|---|---|
| 人为延迟 | `MOCK_LATENCY_MS` | `X-Mock-Latency-Ms` |
| 列表返回空 | `MOCK_EMPTY` | `X-Mock-Empty` |
| 返回 429 | `MOCK_RATE_LIMITED` | `X-Mock-Rate-Limited` |
| 返回 500 | `MOCK_ERROR` | `X-Mock-Error` |
| 开放 `/v1/dev/reset` | `DEV_RESET_ENABLED` | — |

`/health` 不受限流与错误开关影响 —— 它就是用来在异常开关打开时确认服务状态的。

开发重置需要 `X-Admin-Token`（默认 `dev-admin-token`），未开启开关时返回 403，令牌不匹配返回 401。

## 数据与素材

- `app/fixtures/data/*.json` 由 `scripts/generate_fixtures.py` 生成，全部为**编造的占位数据**，不对应任何真实作品或作者。
- `app/static/images/*.jpg` 由 `scripts/generate_sample_images.py` 用 Pillow **本地程序化绘制**，不使用任何第三方素材，已 gitignore。
- 两者的时间戳都按「距今 N 小时」动态计算，因此「今日更新」「最近发布」永远有意义。

## 契约守卫

```powershell
python scripts/contract_diff.py              # 比对服务端 OpenAPI 与 Flutter 仓库的冻结契约
python scripts/export_openapi.py --check     # 同上，并给出导出摘要
python scripts/export_openapi.py             # 额外写下本仓库 openapi.json
python scripts/export_openapi.py --sync      # 比对通过后写回 Flutter 仓库的契约快照
```

比对规则：契约声明的路径、操作、参数、请求体、响应状态、schema、安全要求都必须存在且兼容，缺失即失败；
服务端多出的内容（额外 schema、额外状态码）只报警告，因为对客户端无害。
`tests/test_contract_sync.py` 会在 `pytest` 中执行同一份比对，契约漂移会直接让测试失败。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest
```

覆盖：分页边界与游标作用域、检索匹配（分词 / 拼音 / 别名）、搜索空结果、筛选组合、匿名用户隔离、鉴权失效（含 `TOKEN_EXPIRED`）、
收藏幂等、图片代理各变体、每日冷知识稳定性、宠物对话专家路由与安全拦截、模拟开关、CORS、契约同步、
B 站适配器（id 映射 / 质量筛选 / 风控与缓存回落 / 封面代理）、以及逐条校验产出 `ContentItem` 的 payload 形状。

## 安全与合规

- 不提供注册、登录、跨设备同步与令牌刷新；`install_id` 仅用于重装后复用匿名用户，不使用 IDFA / Android ID 等可追踪标识。
- 第三方凭据（如 `PIXIV_COOKIE`）只允许放在服务端环境变量或本地 `.env`，**绝不进入客户端、绝不入库**。
- 敏感内容过滤在服务端强制生效，不依赖上游未登录时的天然隔离。
- 不轮换账号、不自动登录；上游失效时返回 `503 UPSTREAM_UNAVAILABLE`，`/health` 置为 `degraded`。

## P1（未实现）

`repositories/mysql/` 提供仓储接口骨架与 `schema.sql`，设置 `DATABASE_URL` 后按同一接口落地即可，
业务层无需改动；未配置时自动使用内存实现。同理，`app/services/catalog.py` 是内容源的唯一分派点
（真实适配器在 `app/services/pixiv/` 与 `app/services/bilibili/`）。

## 真实内容源（Pixiv）

默认 `CONTENT_SOURCE=mock`：全部接口由本地夹具供数，契约、测试与不带网络的环境行为完全一致。
需要真实内容时在 `.env` 里切换：

```dotenv
CONTENT_SOURCE=pixiv
PIXIV_PROXY=http://127.0.0.1:7897     # 国内直连基本不可达，填本机代理
PIXIV_COOKIE=<浏览器登录 pixiv 后复制的完整 Cookie>   # 只存服务端
```

切换后 `/v1/images/recommended`、`/v1/images/daily`、`/v1/images/search`、`/v1/images/{id}`、
`/v1/images/{id}/file` 改由 `app/services/pixiv/` 供数，客户端与契约零改动：

- 内容 ID 前缀 `px_<pid>`（如 `px_149750971`），与夹具 ID `img_0001` 天然隔离；
- 图片必须经本服务代理：`i.pximg.net` 强校验 `Referer`，客户端直连必然 403；
- 敏感内容判定组合 `xRestrict` + `illust_content_type` + 标签兜底（**不看 `sl`**，实测普通插画也会 `sl=2`）；
- 429 读 `Retry-After` 后指数退避 + 抖动，不轮换账号；401/403/3xx 跳登录页统一映射成 `503 UPSTREAM_UNAVAILABLE`；
- 登录态缺失或最近一次上游调用失败时 `/health` 的 `status` 置为 `degraded`（不主动探测）。

真实链路冒烟（只读、低量，需要网络）：

```powershell
$env:PIXIV_PROXY = "http://127.0.0.1:7897"
.venv\Scripts\python.exe scripts\pixiv_smoke.py --mode daily --limit 3

# 只诊断 R18 / 动图筛选在当前登录态下是否真的生效（打印上游 xRestrict / illustType 分布）
.venv\Scripts\python.exe scripts\pixiv_smoke.py --check-filters --keyword "初音ミク"
```

未实现：pixiv 书签同步；图片字节无磁盘缓存。

## 真实视频源（B 站）

视频与图片是**两个独立的内容源开关**：`CONTENT_SOURCE` 管图片，`VIDEO_SOURCE` 管 `/v1/daily/bilibili`。

```dotenv
VIDEO_SOURCE=bilibili
BILIBILI_PROXY=http://127.0.0.1:7897     # 本机代理；直连能通就留空
# 可选项
BILIBILI_COOKIE=<风控收紧时填浏览器 Cookie（buvid3 等），只存服务端，永不进客户端>
BILIBILI_LIKE_RATE_THRESHOLD=0.1         # 质量阈值：点赞率 = 点赞数 / 播放数
BILIBILI_CACHE_TTL_SECONDS=600           # 热门页 / 分类榜缓存
BILIBILI_COVER_TTL_SECONDS=3600          # 封面地址缓存
BILIBILI_MIN_INTERVAL_SECONDS=0.5        # 两次上游调用之间的最小间隔
```

切换后 `/v1/daily/bilibili`、`/v1/images/{id}`、`/v1/images/{id}/file` 改由
`app/services/bilibili/` 供数，客户端与契约零改动（只多了一个可选查询参数）：

- 内容 ID 前缀 `bv_<bvid>`（如 `bv_BV1Sve26eENf`），与夹具 ID `video_0001` 天然隔离；
- 上游接口**都不需要登录态**：`x/web-interface/popular`（热门）、`x/web-interface/ranking/v2`
  （分类榜）、`x/web-interface/view?bvid=`（单条详情）；
- 质量优先：沿用爬虫的「点赞率 = 点赞数 / 播放数」排序，越过阈值的在前，不够时按点赞率补齐
  （宁可给出榜单本身，也不返回空列表）；
- 封面必须经本服务代理（hdslb 校验 Referer），并按 `@480w_270h_1c.webp` 取小图
  （12-20KB，原图约 230KB）；
- `-352` / `-412`（风控）统一映射成 `503 UPSTREAM_UNAVAILABLE`，上游 `message` 不外泄；
- `ranking/v2` 风控严格：密集请求会持续 `-352`（实测约 10 分钟恢复），因此做了**限速 + 串行 +
  TTL 缓存**，并在风控命中时回落到上一次的陈旧缓存；完全没有缓存时返回 503。

`GET /v1/daily/bilibili` 支持可选 `category`（分区 slug：`all` / `douga` / `music` / `game` /
`knowledge` / `kichiku` / …），留空取热门综合榜；未知 slug 返回 400 `BAD_REQUEST`。
完整分区表见 `app/services/bilibili/adapter.py` 的 `RANKING_CATEGORIES`。

真实链路冒烟（只读、低量，需要网络）：

```powershell
$env:BILIBILI_PROXY = "http://127.0.0.1:7897"
.venv\Scripts\python.exe scripts\bilibili_smoke.py --limit 5
.venv\Scripts\python.exe scripts\bilibili_smoke.py --category game
```

未实现：分区筛选 UI（参数已在服务端与契约里）、视频收藏 / 历史（视频详情跳 B 站官方 App / 网页）、
爬虫的 CSV 落盘与 20 分类批量任务。
