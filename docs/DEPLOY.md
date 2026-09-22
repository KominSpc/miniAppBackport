# 部署到 Linux 服务器（内测 / 生产）

面向「把 `miniAppBackport` 搬到一台云服务器」的完整步骤。本机 Windows 调试用 `pwsh run.ps1`，
服务器上按本文走一遍即可。

## 0. 组件与端口

| 组件 | 目录 | 运行时 | 端口 | 谁访问 |
|---|---|---|---|---|
| 后端（本仓库） | `miniAppBackport/` | Python 3.11+ / FastAPI + uvicorn | 18421 | **客户端直连**，需要对外开放 |
| 音乐上游 | `multiPlatformMusicApi/` | Node.js >= 18 | 19531 | 只给后端调 |
| 漫画上游 | `mini_app/tools/comic_service.py` + `ComicDown/` | Python + Flask | 19631 | 只给后端调 |
| 数据库 | MySQL 8 | — | 3306 | 只给后端调 |

端口刻意选在低频区间（避开 3000/8000/8080）。**只有 18421 需要对外**；19531 / 19631 / 3306
建议只监听内网或 127.0.0.1。

## 1. 服务器要求

- 2 vCPU / 2 GB 内存起步（同时跑后端 + Node 音乐 + 漫画，建议 4 GB）。
- Python 3.11+（本仓库 Dockerfile 用 3.13）、Node.js 18+、MySQL 8。
- 出网要求（按用到的功能）：
  - `www.pixiv.net` / `i.pximg.net`：**国内机器直连基本不可达**，需要服务器侧有代理或换海外节点；
  - 音乐上游所需的网易云接口；
  - `api.deepseek.com`（宠物对话 / 音乐每日推荐用，未配 key 时自动回落规则引擎）。
- 磁盘：代码 + Python 依赖约 300 MB，Node 依赖约 100 MB；图片与音频字节目前只做内存缓存。

## 2. 拉代码

```bash
sudo mkdir -p /opt/miniapp && cd /opt/miniapp
git clone git@github.com:KominSpc/miniAppBackport.git
git clone https://github.com/<你的音乐上游仓库> multiPlatformMusicApi
git clone https://github.com/<ComicDown 上游仓库> ComicDown
git clone <Flutter 客户端仓库> MiniApp        # 只需要 tools/comic_service.py
```

> `.env` 被 `.gitignore` 排除，**不会随 clone 下来**：从本机 `miniAppBackport/.env` 拷一份到服务器，
> 权限设成 `chmod 600`。里面的 `LLM_API_KEY` / `PIXIV_COOKIE` 属于第三方凭据，不要放进任何仓库。

## 3. 后端安装与启动

```bash
cd /opt/miniapp/miniAppBackport
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 只有 CONTENT_SOURCE=mock（本地夹具供数）时才需要下面两步
.venv/bin/python scripts/generate_fixtures.py
.venv/bin/python scripts/generate_sample_images.py

# 前台试跑
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 18421 --env-file .env
```

`requirements.txt` 与 `pyproject.toml` 两份依赖是一致的（后者 `requires-python = ">=3.11"`）。
`uvicorn[standard]` 自带 python-dotenv，所以 `--env-file .env` 就能加载配置；同名环境变量优先级更高，
临时覆盖用 `KEY=value .venv/bin/python -m uvicorn ...` 即可。

验收：

```bash
curl -s http://127.0.0.1:18421/health          # {"data":{"status":"ok"|"degraded",...}}
curl -s http://127.0.0.1:18421/docs >/dev/null # 接口文档
```

`status` 为 `degraded` 表示「配置的真实上游最近一次调用失败」（例如 pixiv 没代理 / Cookie 过期），
接口本身仍可用，只是相关列表会走回落。

## 4. 数据库（MySQL）

```sql
CREATE DATABASE miniapp CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'miniapp'@'%' IDENTIFIED BY '<强密码>';
GRANT ALL PRIVILEGES ON miniapp.* TO 'miniapp'@'%';
FLUSH PRIVILEGES;
```

`.env` 里写：

```dotenv
DATABASE_URL=mysql://miniapp:<强密码>@127.0.0.1:3306/miniapp?charset=utf8mb4
```

- **密码里的特殊字符要 percent-encode**：`@` → `%40`，`:` → `%3A`，`#` → `%23`。
- 表结构由启动时的 `ensure_schema()` 按 `app/repositories/mysql/schema.sql` **自动创建（幂等）**，
  不需要手工建表：`anon_user` / `favorite` / `favorite_snapshot` / `history_entry` /
  `music_playlist` / `music_playlist_item` / `conversation` / `chat_message` / `pet_affection` 共 9 张。
- 时间列一律按 UTC 存、读取时转回 `Asia/Shanghai`；服务器时区设成 UTC 也不用改代码。
- 留空 / 注释掉 `DATABASE_URL` 时后端自动退回内存实现（重启即丢数据），本地调试可以这样。

## 5. `.env` 关键项

以 `.env.example` 为模板。服务器上至少要确认这几项：

| 键 | 建议值 | 说明 |
|---|---|---|
| `CONTENT_SOURCE` | `pixiv` | `mock` = 本地夹具；`pixiv` = 真实图源 |
| `PIXIV_COOKIE` | 完整 Cookie | R18 / 收藏数 / 语言筛选生效的前提；只在服务端 |
| `PIXIV_PROXY` | 留空或代理地址 | 留空 = 系统代理 → 直连；服务器没有代理就留空，别写死本机地址 |
| `MUSIC_SOURCE` | `node` | 配合音乐上游服务 |
| `MUSIC_API_BASE` | `http://127.0.0.1:19531` | 音乐上游地址 |
| `MUSIC_STREAM_SECRET` | 换成长随机串 | 播放地址签名密钥；换掉等于让旧地址立刻失效 |
| `MUSIC_DEFAULT_LEVEL` | `exhigh` | 默认最高音质（上游未登录时会回落 128k） |
| `COMIC_SOURCE` | `node` | 配合漫画上游服务 |
| `COMIC_API_BASE` | `http://127.0.0.1:19631` | 漫画上游地址 |
| `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | deepseek 那套 | 宠物对话与音乐每日推荐；留空即回落 |
| `DATABASE_URL` | 见上一节 | 不配就退内存 |
| `PUBLIC_BASE_URL` | 留空或 `https://域名` | 留空时按请求 Host 生成图片 / 音频链接 |
| `CORS_EXTRA_ORIGINS` | 逗号分隔 | 只对浏览器（Flutter web）联调有意义；安卓端不需要 |
| `DEV_RESET_ENABLED` | **`false`** | 线上必须关：它开放 `POST /v1/dev/reset`（清空收藏 / 历史 / 会话） |
| `TOKEN_TTL_HOURS` / `RATE_LIMIT_PER_MINUTE` | 720 / 600 | 匿名令牌有效期与限流 |
| `MOCK_*` | 全 `false` | 人为延迟 / 空列表 / 429 / 500，线上不要开 |

## 6. systemd（后端 / 音乐 / 漫画三个服务）

`/etc/systemd/system/miniapp-backend.service`：

```ini
[Unit]
Description=miniApp backend (FastAPI)
After=network.target mysql.service

[Service]
Type=simple
User=miniapp
WorkingDirectory=/opt/miniapp/miniAppBackport
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/miniapp/miniAppBackport/.venv/bin/python -m uvicorn app.main:app \
  --host 0.0.0.0 --port 18421 --env-file /opt/miniapp/miniAppBackport/.env
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/miniapp-music.service`：

```ini
[Unit]
Description=miniApp music upstream (Node)
After=network.target

[Service]
Type=simple
User=miniapp
WorkingDirectory=/opt/miniapp/multiPlatformMusicApi
Environment=PORT=19531
Environment=HOST=127.0.0.1
Environment=NODE_ENV=production
ExecStart=/usr/bin/node app.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/miniapp-comic.service`（`comic_service.py` 只从 ComicDown 借 `onepiece` 爬虫包）：

```ini
[Unit]
Description=miniApp comic upstream (Flask + ComicDown crawler)
After=network.target

[Service]
Type=simple
User=miniapp
WorkingDirectory=/opt/miniapp/MiniApp
Environment=COMIC_DIR=/opt/miniapp/ComicDown
Environment=COMIC_PORT=19631
ExecStart=/opt/miniapp/MiniApp/.venv/bin/python tools/comic_service.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now miniapp-backend miniapp-music miniapp-comic
systemctl status miniapp-backend --no-pager
journalctl -u miniapp-backend -n 50 --no-pager
```

### 上游服务各自的安装命令

```bash
# 音乐上游
cd /opt/miniapp/multiPlatformMusicApi
npm ci --omit=dev            # 只装运行依赖；npm install 会连 eslint/jest 一起装
cp .env.example .env         # PORT=19531 HOST=0.0.0.0 NODE_ENV=production

# 漫画上游：装 ComicDown 的爬虫依赖（不需要 Flask-SQLAlchemy，见 tools/comic_service.py 顶部说明）
cd /opt/miniapp/ComicDown
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install Flask
# 客户端仓库自己的 venv（comic_service.py 跑在它里面）
cd /opt/miniapp/MiniApp
python3 -m venv .venv && .venv/bin/pip install Flask
```

> `comic_service.py` 默认 `COMIC_DIR=D:\flutterobj\ComicDown`（Windows 路径），**Linux 上必须**用
> `COMIC_DIR` 环境变量指到实际路径，否则启动即报「找不到 ComicDown 仓库」。
> `PyExecJS` 需要一个 JS 运行时，服务器上装了 Node 就够。

## 7. 反向代理与 HTTPS（可选）

安卓端直连 `http://<IP>:18421` 就能用；要域名 + HTTPS 时用 nginx：

```nginx
server {
    listen 443 ssl http2;
    server_name api.example.com;
    # ssl_certificate ...; ssl_certificate_key ...;

    location / {
        proxy_pass http://127.0.0.1:18421;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        # 图片 / 音频是流式响应：不要开缓冲，否则大图与音乐会被攒着再发
        proxy_buffering off;
        proxy_read_timeout 120s;
    }
}
```

`PUBLIC_BASE_URL` 填 `https://api.example.com` 时，返回的 `cover_url` / `stream_url` 就都用域名拼；
留空则按客户端请求的 Host 生成（本机、局域网真机都能直接测）。

## 8. 安全组 / 防火墙

- 放行：`22`（SSH）、`443`（走 nginx 时）、`18421`（不走 nginx 时必须放行，建议只放行测试机 IP）。
- **不要**对公网放行 `3306`：数据库与本机后端同机时用 `127.0.0.1`；要远程连就按来源 IP 授权。
- `19531` / `19631` 只监听 `127.0.0.1`（systemd 里已按这个配）。

## 9. 迁移后的验收清单

```bash
# 1) 健康检查
curl -s http://127.0.0.1:18421/health

# 2) 匿名登录拿令牌
curl -s -X POST http://127.0.0.1:18421/v1/users/anonymous \
  -H 'Content-Type: application/json' \
  -d '{"install_id":"probe-1","platform":"desktop","app_version":"1.0.0"}'

# 3) 契约校验（可选，确认服务端与客户端契约一致）
cd /opt/miniapp/miniAppBackport && .venv/bin/python scripts/contract_diff.py

# 4) 全量测试（可选，确认依赖装全）
.venv/bin/python -m pytest -q
```

客户端把后端地址改成 `http://<服务器IP>:18421`（或 `https://api.example.com`）即可联调。

## 10. 升级、备份与排障

```bash
# 升级
cd /opt/miniapp/miniAppBackport && git pull
sudo systemctl restart miniapp-backend

# 数据库备份
mysqldump -u root -p --single-transaction --default-character-set=utf8mb4 miniapp > miniapp-$(date +%F).sql

# 排障
journalctl -u miniapp-backend -n 200 --no-pager
journalctl -u miniapp-music -n 100 --no-pager
```

常见现象：

| 现象 | 原因 | 处理 |
|---|---|---|
| `/health` 一直 `degraded`，推荐流为空 | 服务器直连不到 pixiv | 服务器侧配代理（`PIXIV_PROXY`）或换海外节点 |
| 图片 / 音乐 502、超时 | 上游被墙或音乐上游没起来 | `curl` 一下 19531 / 19631，看 `journalctl` |
| 收藏、歌单重启就没了 | `DATABASE_URL` 没配 | 按第 4 节配库并重启 |
| 播放地址立刻 403 | `MUSIC_STREAM_SECRET` 被改过，旧签名失效 | 正常现象，客户端重新解析即可 |
| 宠物对话变成固定话术 | `LLM_API_KEY` 缺失或不可达 | 补 key；回落规则引擎不影响其它功能 |

## 11. 已知限制

- pixiv 需要服务器能出网到日本：国内机器没有代理时，推荐流为空、R18 / 收藏数 / 语言筛选不生效（这是上游行为，不是本服务的问题）。
- 图片与音频字节只在内存缓存，没落磁盘；重启后首次访问会重新回源。
- 未实现注册 / 登录 / 跨设备同步；`install_id` 只用于重装后复用同一个匿名用户。
- 音乐上游与漫画上游都是第三方项目，升级时注意它们的破坏性变更。
