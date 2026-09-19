# miniAppBackport

「二次元美图与每日内容应用」的**模拟服务**（契约实现方）。

Flutter 客户端仓库：`KominSpc/MiniApp`，其 `contract/openapi.json` 是客户端-服务端契约的单一事实来源。
本仓库按该契约实现全部 `/v1` 接口，供本地与局域网联调使用；后续接入真实上游（Pixiv / B 站 / 游戏库）时，路径与响应结构保持不变。

## 快速开始

```powershell
pwsh run.ps1 -Init      # 建 venv、装依赖、生成夹具与示例图片
pwsh run.ps1            # 启动 http://127.0.0.1:8000
pwsh run.ps1 -Reload    # 开发模式，自动重载
pwsh run.ps1 -Check     # 只做契约校验
```

或使用容器：

```powershell
docker compose up --build
```

启动后：

- 接口文档：`http://127.0.0.1:8000/docs`
- 健康检查与当前开关：`GET /health`
- 局域网真机联调：服务默认监听 `0.0.0.0`，手机访问 `http://<本机IP>:8000`

## 目录结构

```
app/
  main.py            应用装配：中间件、异常处理、CORS
  config.py          配置与模拟开关（环境变量 + 请求头）
  deps.py            依赖注入：配置、模拟开关、Bearer 鉴权、分页参数
  core/              信封、错误码、分页游标、时区、限流、空数据中间件
  schemas/           契约模型：枚举、Meta/ErrorInfo、ContentItem、各接口模型与 14 个信封
  api/v1/            system / users / images / daily / games / pet / dev 路由
  services/          业务编排：编目、互动、每日、宠物
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

覆盖：分页边界与游标作用域、搜索空结果、筛选组合、匿名用户隔离、鉴权失效（含 `TOKEN_EXPIRED`）、
收藏幂等、图片代理各变体、每日冷知识稳定性、宠物对话专家路由与安全拦截、模拟开关、CORS、契约同步、
以及逐条校验产出 `ContentItem` 的 payload 形状。

## 安全与合规

- 不提供注册、登录、跨设备同步与令牌刷新；`install_id` 仅用于重装后复用匿名用户，不使用 IDFA / Android ID 等可追踪标识。
- 第三方凭据（如 `PIXIV_COOKIE`）只允许放在服务端环境变量或本地 `.env`，**绝不进入客户端、绝不入库**。
- 敏感内容过滤在服务端强制生效，不依赖上游未登录时的天然隔离。
- 不轮换账号、不自动登录；上游失效时返回 `503 UPSTREAM_UNAVAILABLE`，`/health` 置为 `degraded`。

## P1（未实现）

`repositories/mysql/` 提供仓储接口骨架与 `schema.sql`，设置 `DATABASE_URL` 后按同一接口落地即可，
业务层无需改动；未配置时自动使用内存实现。同理，`app/services/catalog.py` 是接入 Pixiv / B 站真实适配器的唯一落点。
