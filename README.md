# Pixiv Library MCP — v0.3.2（API 优先 + HTTP Bearer 鉴权）

Pixiv 插画、漫画、小说的搜索、详情、阅读与批量下载 MCP。以 [Pixev/PixEz](https://github.com/dandibbert/pixez-flutter) 实际实现为参照：优先使用 Pixiv App API 自身的过滤和排序能力；只有 API 不提供的条件或被明确拒绝、且能够准确用作品元数据判断的条件才使用本地后筛选。保留 v0.2.1 的下载、完整漫画分页和小说 TXT/EPUB 功能。

## v0.3.2 新增：Streamable HTTP 原生 Bearer Token 鉴权

- **stdio 保持原样**：默认 `uv run pixiv-mcp-library`，无需 HTTP Token；凭据只用于访问 Pixiv。
- **Streamable HTTP**：`--transport http`，标准 MCP 路径为 `/mcp`，支持 HTTP `GET` / `POST` / `DELETE`。
- **必需鉴权**：HTTP 启动要求至少 32 位的 `PIXIV_MCP_BEARER_TOKEN`。无 Token、错误 Token、重复 Authorization Header 都会返回 `401 Unauthorized`，附 `WWW-Authenticate: Bearer`。不允许把 Token 放进 URL / Cookie 替代 Header。
- **远程部署**：`--host 0.0.0.0` 或 IPv6 `::` 允许远程连接，但必须提供一个非本地的 Host 允许名单；未列入的 Host 返回 `421`，未授权的 `Origin` 返回 `403`。SDK 自身的 DNS rebinding 防护仍会启用。
- **完整保护**：所有 HTTP 请求先经过鉴权，再进入 MCP；没有不受保护的工具、下载任务或调试接口。HTTP MCP Token 与 Pixiv OAuth refresh token 完全独立。
- **HTTPS**：可以使用 `--ssl-certfile` + `--ssl-keyfile` 直接开启 TLS，或者放在 HTTPS 反向代理后。若用 HTTP，**仅限受保护的 Tailscale/WireGuard 网络**；不建议把未加密的 Bearer Header 暴露在公网。
- **重要边界**：这是适合个人自部署的**静态 Bearer Token**，**不是 OAuth 2.1 授权服务器**，暂时没有自动登录/授权发现/细粒度权限或在线 Token 轮换机制。更换 Token 后重启服务。本版默认不开放跨域浏览器 CORS；服务端 MCP 客户端无需 CORS。

### 1. 本地 HTTP 启动

先安装依赖（下文有安装命令），在项目的 `.env` 中分别配置两种 Token：

```sh
# 生成强随机 MCP 访问 Token；复制结果到你的私有 .env 中，勿发到聊天或提交到仓库
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

```ini
PIXIV_REFRESH_TOKEN=你的Pixiv刷新令牌
PIXIV_MCP_BEARER_TOKEN=你刚生成的独立长随机字符串
PIXIV_MCP_ALLOWED_HOSTS=
PIXIV_MCP_ALLOWED_ORIGINS=
```

```sh
uv run pixiv-mcp-library --transport http --host 127.0.0.1 --port 8765
# MCP URL: http://127.0.0.1:8765/mcp
```

HTTP 未提供有效 Token 时**不会启动**。如果仅使用 stdio，则不需要配置 `PIXIV_MCP_BEARER_TOKEN`。

### 2. Bero / Tailscale / HTTPS 远程部署

```ini
# 举例：只允许这两个 Host，且自动保留 localhost/127.0.0.1
PIXIV_MCP_ALLOWED_HOSTS=100.101.102.103,mcp.example.com
```

```sh
# 监听所有 IPv4 网卡；通过防火墙限制入口到 Tailscale，或使用 HTTPS 代理
uv run pixiv-mcp-library --transport http --host 0.0.0.0 --port 8765

# 也可直接启用 TLS（证书路径替换为自己的）
uv run pixiv-mcp-library --transport http --host 0.0.0.0 --port 8765 \
  --ssl-certfile /secure/fullchain.pem --ssl-keyfile /secure/privkey.pem
```

这里 `PIXIV_MCP_ALLOWED_HOSTS` 填写**客户端实际访问时发送的 Host**（域名或 Tailscale IP），不是 `0.0.0.0`。HTTP 反向代理需保留正确的 Host Header。也可以用 `--allow-host mcp.example.com`（可重复）替代环境变量。默认允许同源 Origin，跨域 Origin 可通过 `PIXIV_MCP_ALLOWED_ORIGINS` 或 `--allow-origin` 显式列出（但**仅放行 Origin 不等于启用 CORS**）。

一个支持自定义 HTTP 请求头的 MCP 客户端应使用：

```json
{
  "mcpServers": {
    "pixiv": {
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <你的独立MCP访问Token>"
      }
    }
  }
}
```

这是**通用配置示意**，Codex／Paseo／Claude 等客户端的实际配置文件字段可能不同；以客户端当前版本为准。也可以用 `curl` 测拒绝效果：

```sh
# 不带 Authorization 必须返回 401；不要把真实 Token 写进命令行历史
curl -i http://127.0.0.1:8765/mcp
```

**安全提醒**：公网部署要配置 HTTPS 和访问控制（或在私有 VPN 内），并保护 `.env` 文件权限。`Authorization` 包含的是 MCP 静态 Bearer，不是 Pixiv `refresh_token`。

---

> **验证状态：模拟测试通过，真实账号尚未验收。** 由于没有用户的 Pixiv Token，也无法在本环境安装 PyPI 上的 MCP/PixivPy3 依赖，目前不能保证所有接口在当前 Pixiv 服务端对免费／Premium 账号都可用。代码已经按 Pixev 的原生请求参数实现；遇到 API 拒绝会明确报错或标记回退，不会默默忽略要求。请不要把 `refresh_token` 发到聊天里。

## v0.3 新增：API 优先搜索

| 筛选 / 排序 | 插画、漫画 | 小说 | 具体处理 |
|---|---|---|---|
| 热度 `popular_desc` | ✅ | ✅ | Pixiv 服务端排序；通常需 Premium |
| 男性／女性热门排序 | ✅ | — | `popular_male_desc` / `popular_female_desc`，视账号权限 |
| 非会员热门预览 | ✅ | ✅* | `popular_preview` 独立有限样本；小说端点需真实验收 |
| 日期新旧、发布时间范围 | ✅ | ✅ | Pixiv 原生 `sort`、`start_date`、`end_date` |
| 关键词／标签匹配 | ✅ | ✅ | Pixiv 原生 `search_target`；小说额外支持 `keyword`、`text`（正文） |
| 收藏数上下限 | ✅ | ✅ | 直接传 `bookmark_num_min`、`bookmark_num_max`；小说另兼容 `bookmark_num` |
| 搜索语言 `lang` | — | ✅ | 直接传 `lang`（Pixev 明确有 `ja`、`zh-CN`） |
| 最少字数 | — | ✅ | 原生 `text_length_min`；最多字数仍为本地筛选 |
| AI 作品 | ✅ | ✅ | `ai_mode=exclude` 原生发送 `search_ai_type=0`，`only` 发送 1 后本地只保留 AI |
| 仅原创小说 | — | ✅ | `originals_only=true` 原生 `is_original_only=true`；`false` 本地排除原创 |
| 仅可替换文本小说 | — | ✅ | `is_replaceable_only=true` 原生请求，无法可靠本地回退 |
| 包含可能违规作品 | — | ✅ | `include_potential_violation_works` 原生布尔开关 |
| 翻译标签／合并关键词结果 | ✅ | ✅ | 可传 `include_translated_tag_results`、`merge_plain_keyword_results` |
| 最近 1 天／周／月 | ✅ | 需日期范围 | 普通插画搜索原生 `duration`，热门预览本地按日期过滤 |
| 浏览数、页数、收藏状态、标签排除 | 本地 | 本地 | App API 无对应可靠搜索参数，分页扫描后过滤 |
| R-18 / R-18G、封面与完整元数据 | 本地筛选 | 本地筛选 | 根据 `x_restrict` 等 API 返回字段；不改变账号访问权限 |
| `bookmarks_desc` / `views_desc` | 局部排序 | 局部排序 | 仅在已扫描的结果窗口内排序，不是 Pixiv 全局热度 |

* `popular_preview` 不是 `popular_desc`：它只返回有限预览，不能承诺可以像会员热度搜索一样连续翻页。它也**不会静默忽略**小说 `lang` 等只能在普通搜索接口保证的条件，这类组合会直接提示换用 `popular_desc` 或 `date_desc`。

### 为什么要绕过 PixivPy3 的高层搜索包装？

Pixev 的 `getSearchIllust` 和 `getSearchNovel` 已支持 `lang`、`bookmark_num_min/max`、`text_length_min` 等参数，但 PixivPy3 的便捷方法没有全部暴露。v0.3 使用同一 PixivPy3 已认证会话，直接向 `/v1/search/illust` 或 `/v1/search/novel` 发送这些参数。尤其是 PixivPy3 原有判断会把数值 `0` 的 `search_ai_type` 漏掉；v0.3 通过原生请求确保 `0` **真正发送到 Pixiv**。普通、无额外原生选项的请求继续使用 SDK 便捷方法。

- **API 正常响应**：保留 Pixiv 服务端排序及过滤；返回 `filter_execution.native_requested` 说明本次送往 Pixiv 的字段，同时本地会对可读取的元数据做安全复核。
- **原生参数明确不支持，且本地有足够元数据**：可降级为普通 SDK 搜索，再本地执行收藏数范围、最少字数、原创或 AI 排除条件；在 `filter_execution.fallback_to_local` 中列出降级项。
- **语言、可替换文本、可能违规作品等无法准确本地复现的选项**：若 Pixiv 拒绝原生请求，会**明确报错**，不会返回冒充“已筛选”的结果。网络断开、429、认证错误也不会被当作普通本地回退。
- **`source=user/bookmarks/series`**：它们是列表 API，不是搜索 API。可根据已返回作品元数据进行收藏数、字数、标签等筛选，并持续翻页寻找匹配项；`lang` 等原生搜索专属条件会被拒绝，避免假筛选。

注意：`native_requested` 表示请求已经发送，并不证明 Pixiv 上游真正支持所有语言代码或权限组合；这仍需要真实账号测试。对于 `lang`，目前仅确认 Pixev UI 已使用 `ja`、`zh-CN`；其他 BCP47 代码可以透传，但是否有效取决于 Pixiv。

### 高级小说搜索示例（API 排序 + 语言 + 收藏数 + 字数）

`pixiv_search_advanced`：

```json
{
  "query": "五悠",
  "kind": "novel",
  "view": "full",
  "page_size": 20,
  "search_target": "keyword",
  "sort": "popular_desc",
  "lang": "ja",
  "min_bookmarks": 100,
  "max_bookmarks": 20000,
  "min_text_length": 5000,
  "max_text_length": 50000,
  "ai_mode": "exclude",
  "include_translated_tag_results": true,
  "merge_plain_keyword_results": true
}
```

其中 `sort`、`lang`、收藏数范围、最少字数、AI 排除都会优先走 Pixiv API；`max_text_length` 会在检索后的本地判断。小说按正文搜索可改为 `search_target="text"`。

### 搜索和批量下载使用同一套筛选条件

`pixiv_start_download(source="search", kind="novel", ...)` 的 `filters` 接受相同高级选项，包括 `lang`（也接受 JSON 别名 `language`）、`min_bookmarks`、`sort`、`min_text_length` 等，重新拉取作品清单再批量保存。

```json
{
  "source": "search",
  "kind": "novel",
  "query": "五悠",
  "limit": 100,
  "novel_format": "both",
  "filters": {
    "lang": "ja",
    "sort": "popular_desc",
    "min_bookmarks": 100,
    "min_text_length": 5000
  }
}
```

`source="search"` 可在三类作品上使用搜索条件；`kind="all"` 会分别搜三类，**不会**把各类型的热度混成一个全站排名。用显式 ID 下载时，筛选条件不会被应用，因此 v0.3 会拒绝给 `source="ids"` 额外传筛选项，以免造成误会。

### 搜索返回哪些信息？

每条作品在 `full` 模式下包含 ID、类型、标题、链接、作者 ID／名称／账号／头像、封面／缩略图及多个图片 URL、简介、标签及翻译、收藏／浏览／评论数、收藏状态、发布时间、年龄限制、AI 类型，以及漫画页数／尺寸、小说字数／系列／原创标记。新增 `language`、`is_replaceable` 字段（**仅上游实际返回时才有值**）。插画点赞数可以单独补查，无法获得时为 `null`；`compact` 仅保留关键字段降低模型 Token 开销。

搜索回复除了 `items`、`next_cursor`、`scanned_count`、`sort_scope`，还有 `filter_execution`。示意：

```json
{
  "sort_scope": "pixiv_order",
  "filter_execution": {
    "native_requested": ["search_target", "sort", "lang", "bookmark_num_min", "text_length_min"],
    "local_post_filter": ["max_text_length"],
    "fallback_to_local": []
  }
}
```

`max_scan` 限制每次最多检查多少上游结果，默认 200、最大 1000；即使原生筛选，分页仍应沿 `next_cursor` 继续。对于 `bookmarks_desc`／`views_desc` 的本地排序，`sort_scope="scanned_window"` 明确表示排序只覆盖当前采样窗口。

---

## 安装（macOS / Linux / Windows）

前提：Python 3.10+、`uv` 或 `pip`、Pixiv API 可用网络。

```sh
# 先解压下载得到的 pixiv-mcp-library-v0.3.zip
cd pixiv-mcp-library-v0.3
uv venv
uv pip install -e '.[test]'
cp .env.example .env
```

编辑 `.env`：

```ini
PIXIV_REFRESH_TOKEN=你的RefreshToken
PIXIV_HTTPS_PROXY=http://127.0.0.1:你的HTTP代理端口
PIXIV_DOWNLOAD_DIR=/Users/you/Downloads/Pixiv
PIXIV_MAX_BATCH=300
PIXIV_REQUEST_DELAY=0.5
```

不要把 Token 发到聊天、提交到 GitHub 或放到非可信的 MCP 服务。该原型**不会**从 PixEz 内部自动提取 Token，且不会将刷新后的 Token 自动持久化到 `.env`。可单独通过 `uvx gppt login -j` 获取凭据。

客户端 stdio MCP 配置示例（更换绝对路径；Windows 使用 `.venv\\Scripts\\python.exe`）：

```json
{
  "mcpServers": {
    "pixiv": {
      "command": "/ABS/PATH/pixiv-mcp-library-v0.3/.venv/bin/python",
      "args": ["-m", "pixiv_mcp_library"]
    }
  }
}
```

或者在安装后的项目目录中手动运行：

```sh
uv run pixiv-mcp-library
# HTTP 会要求独立的 PIXIV_MCP_BEARER_TOKEN；默认只监听本地地址：
uv run pixiv-mcp-library --transport http --port 8765
```

## MCP 工具清单

| 工具 | 作用 |
|---|---|
| `pixiv_auth_status` | 验证 Token 和账户 ID，不返回密钥 |
| `pixiv_search_capabilities` | 查看支持的原生搜索、后筛选、排序限制 |
| `pixiv_search_advanced` | 高级搜索、丰富字段、精准游标分页、按需点赞补查 |
| `pixiv_list_works` | 列出作者、收藏、搜索、小说系列中的作品；`filters` 参数可用 |
| `pixiv_work_detail` | 作品完整详情，默认尝试点赞补查 |
| `pixiv_read_novel` | 小说正文分块读取 |
| `pixiv_start_download` | 图片/漫画/小说批量下载，搜索时复用过滤条件 |
| `pixiv_download_status` | 任务进度、失败列表及输出路径 |
| `pixiv_cancel_download` | 请求取消任务 |

## 下载格式与 v0.1 保留的功能

- 插画原图；漫画所有页面（不会把缩略图误当原图）；Ugoira ZIP+帧信息。
- 小说 TXT、EPUB，也可同时保存两种；尽可能保留图片并写入 EPUB。
- 按画师、作品 ID、收藏、搜索结果、小说系列批量；重复文件跳过。
- 失败作品记录、任务取消、下载任务状态查询。
- 单批默认 `limit=100`、最大 300；通过 `PIXIV_MAX_BATCH` 可调整上限（内部最高 1000）。
- 下载任务进度保存在进程内存，**不支持跨服务重启恢复整个任务**；已有完整文件仍会跳过。

目录示意：

```text
Downloads/Pixiv/
  illust/作者ID - 作者名/作品ID - 标题/作品ID_p0.jpg
  manga/作者ID - 作者名/作品ID - 标题/作品ID_p0.jpg ... _pN.jpg
  novels/作者ID - 作者名/作品ID - 标题/
    作品ID - 标题.txt
    作品ID - 标题.epub
    assets/...
  ugoira/.../作品ID_p0.zip
```

## 本地测试

```sh
uv run pytest -q
```

测试使用假 Pixiv API，不使用真实 Token。当前 96 项测试涵盖 SDK/原生 API 参数路由、Pixev 小说语言与收藏区间、AI=0 防丢失、原生参数拒绝时的安全回退、小说语言不可回退的显式错误、分页与批量下载、EPUB/漫画文件、MCP 工具注册映射等。本版还覆盖静态 Bearer 认证、Host/Origin、重复 Header、HTTP 三种方法、lifespan 透传等。实际 Pixiv、PixivPy3 和 MCP SDK 的联网联调仍须在使用者环境验收。

### 其他注意

1. 本项目是非官方 API 实现。功能受 Pixiv 账号权限、API 变化、访问频率与网络限制影响，请尊重作品版权。
2. 只允许下载来自 Pixiv CDN 的图片链接以避免 SSRF；对特殊 CDN 的重定向可能拒绝下载。
3. 大文件默认上限 250MB；已有非空文件视为已下载，并未校验散列完整性。
4. 原型不直接提供 Web UI，搜索封面以 URL 形式返回；不是浏览器里可点击的缩略图画廊。
5. `pixiv_start_download(source="ids")` 中，作品 ID 不携带“小说或插画”的类别，必须明确 `kind`。

## v0.3.2 — App API returns no data but Pixev works

The MCP's PixivPy3 3.7.5 client and Pixev do **not** send identical headers.
PixivPy3 identifies App API data requests as a legacy iOS client; Pixev uses an
Android identity, version and X-Client-* headers. This release defaults to the
Pixev-compatible profile, implemented without making Pixiv credentials public:

```ini
PIXIV_APP_API_PROFILE=pixev
PIXIV_ACCEPT_LANGUAGE=zh-CN
```

To compare, change `PIXIV_APP_API_PROFILE=legacy`, restart the MCP server and run
`pixiv_diagnose_api` under the same account, proxy and network. Return to `pixev`
and repeat. `pixiv_diagnose_api(illust_id=93287125, novel_id=YOUR_NOVEL_ID)`
returns **only** HTTP status, content type, top-level response keys and expected
key presence; no raw payload, Authorization header, token, HTML or full URLs.
Novel ID is optional and should be one accessible to your account.

The `webview_novel` endpoint is hosted on `app-api.pixiv.net`, *not* on
`www.pixiv.net`; an unrelated working `www.pixiv.net/ajax/...` request does not
prove its App API path is returning the intended data. HTTP 200 with missing
`illusts` may be an upstream error envelope, intermediary response, wrong
routing or other API change; do not presume that a missing X-Client header is
conclusive. A Pixev-compatible request *header* does not replicate the
underlying rhttp TLS fingerprint or proxy behavior. No live Pixiv validation
has been performed here.

**Safe troubleshooting:** Compare the redacted `pixiv_diagnose_api` output under
both profiles. If both return unexpected JSON keys despite a valid OAuth token,
inspect the local routing, proxy, TLS/client behavior and upstream response type
with the agent. Do not paste live Pixiv refresh/access tokens or dump raw HTML
containing account information to public logs.
