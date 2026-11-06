"""Pixiv Library MCP: API-first advanced search and bounded bulk downloads."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from .advanced import SearchOptions
from .downloads import JobManager, PixivDownloader
from .engine import PixivClient, PixivEngine
from .http_auth import BearerAuthASGI, HttpAuthConfig

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
logging.basicConfig(level=logging.INFO)
MCP = FastMCP("Pixiv Library v0.3.2", instructions=(
    "Search Pixiv illustrations, manga and novels, including metadata and advanced filters; "
    "browse novels and download originals, full comics and TXT/EPUB novels. "
    "Use pixiv_search_capabilities to discover which filters are Pixiv-native vs local. "
    "Prefer native lang, bookmark ranges, novel length and popular_desc. "
    "Advanced searches return next_cursor; reuse it with exactly the same filters. "
    "Local bookmark/view sorting is only within scanned results, not global ranking. "
    "Bulk downloading writes local files: preview and confirm the scope first. "
    "Never ask users to post passwords or Pixiv refresh tokens in chat."
))
_service: tuple[PixivClient, PixivEngine, JobManager] | None = None


def service() -> tuple[PixivClient, PixivEngine, JobManager]:
    global _service
    if _service is None:
        client = PixivClient(delay=float(os.getenv("PIXIV_REQUEST_DELAY", "0.5")))
        engine = PixivEngine(client)
        downloader = PixivDownloader(engine, Path(os.getenv("PIXIV_DOWNLOAD_DIR", "./downloads")), proxy=client.proxy)
        manager = JobManager(engine, downloader, max_batch=int(os.getenv("PIXIV_MAX_BATCH", "300")))
        _service = client, engine, manager
    return _service


def search_options_from_dict(filters: dict[str, Any] | None = None) -> SearchOptions:
    if filters is not None and not isinstance(filters, dict):
        raise ValueError("filters must be a JSON object or omitted")
    return SearchOptions.create(**(filters or {}))


@MCP.tool()
def pixiv_auth_status() -> dict:
    """Verify configured Pixiv OAuth refresh token; return only account ID (no tokens)."""
    client, _, manager = service()
    return {"authenticated": True, "user_id": client.user_id,
            "download_root": str(manager.downloader.root), "max_batch": manager.max_batch}


@MCP.tool()
def pixiv_diagnose_api(illust_id: int | None = None,
                       novel_id: int | None = None,
                       word: str = "風景") -> dict:
    """Diagnose Pixiv App API requests on this MCP server without revealing secrets.

    Returns only HTTP statuses, response structure and sanitized signs of HTML
    challenges for read-only search, own artwork listing, and optional work IDs.
    Does not return raw response bodies, authentication headers, cookies or tokens.
    Use PIXIV_APP_API_PROFILE=legacy/pixev to A/B test client compatibility.
    """
    client, _, _ = service()
    return client.diagnose_app_api(illust_id=illust_id, novel_id=novel_id, word=word)


@MCP.tool()
def pixiv_search_capabilities() -> dict:
    """Discover advanced Pixiv search parameters and important backend limitations."""
    return {
        "source": "Authenticated Pixiv App API (Pixev-compatible raw options) + bounded local fallback",
        "kinds": ["illust", "manga", "novel", "all"],
        "native_pixiv_filters": {
            "search_target": {
                "artwork": ["partial_match_for_tags", "exact_match_for_tags", "title_and_caption"],
                "novel": ["partial_match_for_tags", "exact_match_for_tags", "keyword", "text"],
                "all": ["partial_match_for_tags", "exact_match_for_tags"],
            },
            "sort": {
                "artwork": ["date_desc", "date_asc", "popular_desc", "popular_male_desc", "popular_female_desc"],
                "novel": ["date_desc", "date_asc", "popular_desc"],
                "non_premium_preview": "popular_preview (illust/manga/novel; limited one-page selection)",
                "notes": "popular_desc (and gender sorts) require Pixiv Premium; popular_preview is a separate limited API, not complete ranking",
            },
            "date": "start_date / end_date: YYYY-MM-DD",
            "duration": "artwork only: within_last_day/within_last_week/within_last_month",
            "bookmark_range": "min_bookmarks/max_bookmarks sent as bookmark_num_min/bookmark_num_max for artwork and novel searches (Premium/access restrictions may apply)",
            "novel": {
                "lang": "novel search language code (Pixev confirms ja, zh-CN; other BCP47 values forwarded but upstream compatibility must be tested)",
                "min_text_length": "server-side text_length_min; max_text_length remains local",
                "originals_only": "true -> is_original_only=true; false -> local excludes originals",
                "is_replaceable_only": "Pixev App API is_replaceable_only=true",
                "include_potential_violation_works": "Pixev App API native true/false flag",
            },
            "other": {
                "ai_mode": "exclude -> search_ai_type=0; only -> search_ai_type=1 plus local AI-only check",
                "include_translated_tag_results": "Pixiv search include_translated_tag_results=true/false",
                "merge_plain_keyword_results": "Pixiv search merge_plain_keyword_results=true/false",
            },
        },
        "local_filters": ["min_views", "max_views", "min_pages", "max_pages",
                          "max_text_length", "rating (all/safe/r18/r18g)",
                          "ai_mode=only (native API can include AI but not select ONLY AI)",
                          "bookmarked (true/false)", "include_tags", "exclude_tags",
                          "originals_only=false; if raw API unavailable, min/max_bookmarks, min_text_length, originals_only=true and ai_mode=exclude may fall back locally"],
        "local_sort": ["bookmarks_desc", "views_desc"],
        "paging": "next_cursor + page_size 1..50; max_scan bounds the number of upstream entries inspected",
        "views": ["full", "compact"],
        "likes": "total_likes present when upstream supplies it. include_likes=true triggers slow extra reads "
                 "(novel webview or public artwork AJAX); unavailable remains null.",
        "limitations": [
            "Pixiv may not return likes for all works; the public Web AJAX may reject unauthenticated requests",
            "Bookmark/view sorting is within the scanned window, not all Pixiv works",
            "Popular-preview is a limited non-Premium preview, not an unlimited popular_desc search",
            "kind=all Premium popular_desc is separately ordered within each media type, not globally",
            "Filters and search results are limited to the scanned API pages, not a global matching count",
            "Pixev novel lang and bookmark range options are passed directly to Pixiv App API and require live validation",
            "Novel lang/is_replaceable_only/violation flags cannot be faithfully replaced with a local filter; rejection is explicit",
            "Language filtering applies to novel search only, not arbitrary user/bookmarks/series list sources",
            "The finite non-Premium popular_preview endpoint does not promise advanced lang/bookmark/length server filters",
            "Thumbnail/cover URLs are Pixiv CDN URLs; image clients may require Referer: https://www.pixiv.net/",
            "Access to adult-restricted and private works depends on the authenticated account",
        ],
    }


@MCP.tool()
def pixiv_search_advanced(
    query: str,
    kind: str = "illust",
    view: str = "full",
    page_size: int = 20,
    cursor: str | None = None,
    max_scan: int = 200,
    search_target: str = "partial_match_for_tags",
    sort: str = "date_desc",
    start_date: str | None = None,
    end_date: str | None = None,
    duration: str | None = None,
    min_bookmarks: int | None = None,
    max_bookmarks: int | None = None,
    min_views: int | None = None,
    max_views: int | None = None,
    min_pages: int | None = None,
    max_pages: int | None = None,
    min_text_length: int | None = None,
    max_text_length: int | None = None,
    lang: str | None = None,
    rating: str = "all",
    ai_mode: str = "any",
    bookmarked: bool | None = None,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    originals_only: bool | None = None,
    is_replaceable_only: bool | None = None,
    include_potential_violation_works: bool | None = None,
    include_translated_tag_results: bool | None = None,
    merge_plain_keyword_results: bool | None = None,
    include_likes: bool = False,
) -> dict:
    """Advanced Pixiv illustration/manga/novel search with rich metadata and safe paging.

    API-native: search_target, sort=date_desc/date_asc or Premium popular_desc
    for artwork AND novels. Novel lang, bookmark ranges, text_length_min,
    is_original_only, replaceable and violation flags are sent directly to
    Pixiv App API as in Pixev. Local filters cover views/pages, maximum novel
    length, bookmarked, tags, rating, and AI-only. The native filter is
    checked locally too when searchable metadata exists. If a native filter
    cannot safely fall back, it fails explicitly instead of being ignored.
    Local sort bookmarks_desc/views_desc is a sampled window, NOT global.
    max_scan bounds upstream records inspected (1..1000).
    `include_likes` adds a slow per-work extra request (page_size <=20), which
    may fail gracefully with total_likes=null. Cursor MUST use same query/filters.
    """
    opts = SearchOptions.create(
        search_target=search_target, sort=sort, start_date=start_date, end_date=end_date,
        duration=duration, min_bookmarks=min_bookmarks, max_bookmarks=max_bookmarks,
        min_views=min_views, max_views=max_views, min_pages=min_pages, max_pages=max_pages,
        min_text_length=min_text_length, max_text_length=max_text_length,
        lang=lang, rating=rating, ai_mode=ai_mode, bookmarked=bookmarked,
        include_tags=include_tags, exclude_tags=exclude_tags,
        originals_only=originals_only, is_replaceable_only=is_replaceable_only,
        include_potential_violation_works=include_potential_violation_works,
        include_translated_tag_results=include_translated_tag_results,
        merge_plain_keyword_results=merge_plain_keyword_results,
        include_likes=include_likes)
    _, engine, _ = service()
    return engine.search_advanced(query=query, kind=kind, page_size=page_size,
                                  cursor=cursor, max_scan=max_scan, options=opts, view=view)


@MCP.tool()
def pixiv_list_works(source: str, kind: str = "all", limit: int = 30,
                     user_id: int | None = None, query: str | None = None,
                     series_id: int | None = None, restrict: str = "public",
                     filters: dict[str, Any] | None = None) -> dict:
    """List metadata by source=user/bookmarks/search/series without downloading.

    Filters use the same options as pixiv_search_advanced. For cursor paging
    use pixiv_search_advanced directly instead; this list returns up to `limit`
    matching items and may scan more than limit upstream entries.
    """
    _, engine, _ = service()
    works = engine.list_works(source=source, kind=kind, limit=limit, user_id=user_id,
                              query=query, series_id=series_id, restrict=restrict,
                              options=search_options_from_dict(filters))
    return {"count": len(works), "items": works}


@MCP.tool()
def pixiv_work_detail(work_id: int, kind: str = "artwork", include_likes: bool = True) -> dict:
    """Full illustration/manga/novel metadata; optional extra like-count lookup.

    Like counts are not always available from the Pixiv App API. Extra reads
    can fail (Web API restrictions); then total_likes remains null.
    """
    _, engine, _ = service()
    work = engine.get_work(work_id, kind)
    if include_likes:
        engine._get_likes(work)
    return work


@MCP.tool()
def pixiv_read_novel(novel_id: int, offset: int = 0, max_chars: int = 12000) -> dict:
    """Read a novel in 1..40000 character chunks; offset is a character index."""
    _, engine, _ = service()
    return engine.read_novel(novel_id, offset=offset, max_chars=max_chars)


@MCP.tool()
def pixiv_start_download(source: str, kind: str, ids: list[int] | None = None,
                         user_id: int | None = None, query: str | None = None,
                         series_id: int | None = None, limit: int = 100,
                         restrict: str = "public", novel_format: str = "both",
                         filters: dict[str, Any] | None = None) -> dict:
    """Start bounded bulk download after previewing the search/list result.

    source=ids/user/bookmarks/search/series; kind=illust/manga/novel/all.
    `filters` has the same keys as pixiv_search_advanced, excluding pagination;
    example {"search_target":"exact_match_for_tags","sort":"popular_desc",
             "min_bookmarks":100,"lang":"ja","min_text_length":5000} for novels.
    For source=search, exactly the same filter logic is used when selecting
    works to download. Max items is PIXIV_MAX_BATCH (default 300).
    Do not accept passwords or tokens in prompts. Files stay local.
    """
    _, _, manager = service()
    return manager.start(source=source, kind=kind, ids=ids, user_id=user_id,
                         query=query, series_id=series_id, limit=limit,
                         restrict=restrict, fmt=novel_format,
                         options=search_options_from_dict(filters))


@MCP.tool()
def pixiv_download_status(job_id: str) -> dict:
    """Get download progress, failed IDs and last 30 output folders."""
    return service()[2].status(job_id)


@MCP.tool()
def pixiv_cancel_download(job_id: str) -> dict:
    """Cooperatively cancel the download job after current work/file."""
    return service()[2].cancel(job_id)


def build_http_app(config: HttpAuthConfig):
    """Keep the SDK lifespan intact while authenticating every /mcp request."""
    MCP.settings.transport_security = config.transport_security()
    inner_app = MCP.streamable_http_app()  # includes session manager lifespan
    return BearerAuthASGI(inner_app, config)


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Pixiv Library MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address; 0.0.0.0 requires a remote Host allowlist")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-host", action="append", default=[], help="Allowed HTTP Host header / IP; repeat as needed")
    parser.add_argument("--allow-origin", action="append", default=[], help="Additional allowed browser Origin (does not enable CORS)")
    parser.add_argument("--ssl-certfile", default=None, help="TLS certificate (or terminate TLS at a reverse proxy)")
    parser.add_argument("--ssl-keyfile", default=None, help="TLS private key")
    args = parser.parse_args()
    if args.transport == "stdio":
        MCP.run(transport="stdio")  # no HTTP listener, no HTTP secret needed
        return

    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error("--ssl-certfile and --ssl-keyfile must be configured together")
    try:
        auth = HttpAuthConfig.from_environment(
            os.environ, bind_host=args.host,
            allowed_hosts=args.allow_host, allowed_origins=args.allow_origin,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.host not in {"127.0.0.1", "localhost", "::1", "[::1]"} and not args.ssl_certfile:
        logging.warning("Remote HTTP has no built-in TLS: deploy only behind HTTPS or private Tailscale/WireGuard")

    # Calling uvicorn with the ASGI app (instead of MCP.run) is essential:
    # MCP.run() would serve the unauthenticated underlying app directly.
    uvicorn.run(
        build_http_app(auth), host=args.host, port=args.port,
        ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
        proxy_headers=False, log_level="info",
    )


if __name__ == "__main__":
    main()
