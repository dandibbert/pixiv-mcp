## v0.3.2 (diagnostic compatibility build)

- Compare Pixev's App API request headers with PixivPy3 3.7.5. Default to a
  Pixev-compatible Android identity with `App-Version` and per-request
  `X-Client-Time` / `X-Client-Hash`, without rewriting OAuth or all endpoints.
- `PIXIV_APP_API_PROFILE=legacy|pixev` enables a controlled same-process
  compatibility A/B test after restart. `pixev` is default.
- New read-only `pixiv_diagnose_api` tool probes search, current user's artwork
  list and optionally supplied artwork/novel IDs using the *same authenticated
  SDK session*. Output contains only safe HTTP/JSON structure diagnostics.
- When the App API response lacks expected data, return safe response shape
  instead of only `missing illusts`. Novel parsing failures also include a
  redacted response summary.
- Novel Webview URL is **app-api.pixiv.net/webview/v2/novel**, not `www.pixiv.net/webview/`.
- Real Pixiv account/network verification is still required. Pixev uses rhttp,
  whereas PixivPy3 uses cloudscraper: changed headers do not reproduce a TLS
  fingerprint, network route or upstream protection behavior.

# Changelog

## v0.3.1 — Streamable HTTP + static Bearer auth

- 新增独立的 `PIXIV_MCP_BEARER_TOKEN`；HTTP 模式强制配置 32+ 位 ASCII 随机 Token，缺失/无效时拒绝启动；stdio 无变化。
- 新增纯 ASGI `BearerAuthASGI` 层：校验每个 HTTP GET/POST/DELETE 的 `Authorization: Bearer ...`，拒绝重复或伪造 Header；401 返回 `WWW-Authenticate`，不泄露 Token。
- 新增 `PIXIV_MCP_ALLOWED_HOSTS`、`PIXIV_MCP_ALLOWED_ORIGINS`、`--allow-host`、`--allow-origin`，可配合 `--host 0.0.0.0` 远程部署；Host/Origin 先在网关严格校验，再启用 MCP SDK DNS rebinding 防护。
- HTTP 由 `uvicorn` 直接托管受保护的 FastMCP `streamable_http_app()`，保留 MCP 会话管理器的 lifespan；不再调用无鉴权的 MCP HTTP 入口。
- 支持 `--ssl-certfile` 和 `--ssl-keyfile`，也可放在 HTTPS 反向代理或私有 Tailscale/WireGuard 网络后。
- 增加独立 HTTP 安全单元测试（Host/Origin 伪造、重复 Header、令牌正确/错误/缺失、长连接透传、配置错误）；与 0.3 搜索下载测试一起共 96 项本地测试通过。
- 明确静态 Bearer 与 Pixiv OAuth 不同；**尚未实现 OAuth 2.1 自动授权发现**。没有真实 Pixiv 账号或 MCP SDK 安装的联网端到端验收。

## v0.3.0 — Pixev API-first advanced search

- Pixiv-native bookmark minimum/maximum for artwork and novels, with `bookmark_num` compatibility for novel search.
- Native novel language (`lang`; also `language` alias for JSON `filters`), minimum text length, original-only, replaceable-only, potential-violation switches.
- Native merged keywords and translated-tags switches, optional date ranges, duration (artwork), popularity sorting including novel Premium sorting.
- Correctly sends `search_ai_type=0` for AI exclusion instead of letting PixivPy3 silently omit it; `any` explicitly sends `1`, `only` filters locally.
- Uses PixivPy3's authenticated raw App API for parameters omitted from convenience methods. Does **not** rely on unauthenticated web scraping.
- Explicit fallback only when the local metadata is enough to reproduce a rejected filter, and exposes fallback information in search responses. No silent language filter downgrade.
- Same search options pass through bulk discovery/download. Filters in artist/bookmark/novel-series listings continue paging until enough matches, with bounded pagination.
- Adds comprehensive simulated API compatibility, errors, pagination and download tests. No live Pixiv/Premium account verification.
