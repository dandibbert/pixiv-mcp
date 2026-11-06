"""Pixiv API adapter and listing logic. No secrets or UI dependencies here."""
from __future__ import annotations

import json
import logging
import html
import requests
import uuid
from collections import OrderedDict
from dataclasses import replace
import os
import re
import threading
import time
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlparse

from .advanced import SearchOptions, encode_cursor, decode_cursor, UPSTREAM_SORTS, PREVIEW_SORTS
from .compat import make_app_api_class, inspect_response

LOGGER = logging.getLogger(__name__)


def field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def to_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return {}


def work_summary(obj: Any, kind: str) -> dict[str, Any]:
    """Common stable metadata for illustration, manga and novels.

    A field is null when upstream omitted it; numbers are never invented as zero.
    Pixiv App API normally exposes bookmarks/views but not illustration likes.
    """
    user = field(obj, "user", {}) or {}
    user_avatar = field(user, "profile_image_urls", {}) or {}
    images = field(obj, "image_urls", {}) or {}
    tags = field(obj, "tags", []) or []
    details = [{"name": str(field(tag, "name", tag)),
                "translated_name": field(tag, "translated_name", None)} for tag in tags]
    novel = kind == "novel"
    wid = int(field(obj, "id", 0))
    series = field(obj, "series", None)
    series_id = field(series, "id", None)
    series_data = ({"id": series_id, "title": field(series, "title", "")}
                   if series_id is not None else None)
    raw_caption = field(obj, "caption", "")
    caption = raw_caption if isinstance(raw_caption, str) else ""
    # Description preview is plain text for concise Agent output; caption
    # retains its full HTML/plaintext Pixiv content.
    excerpt = html.unescape(re.sub(r"<[^>]*>", " ", caption)).strip()
    excerpt = re.sub(r"\s+", " ", excerpt)
    count_likes = field(obj, "total_likes", field(obj, "like_count", field(obj, "likeCount", None)))
    if count_likes is None and novel:
        count_likes = field(field(obj, "rating", {}), "like", None)
    return {
        "id": wid,
        "kind": "novel" if novel else field(obj, "type", kind),
        "title": str(field(obj, "title", "")),
        "user_id": str(field(user, "id", "")),
        "user_name": str(field(user, "name", "")),
        "user_account": field(user, "account", None),
        "user_avatar_url": field(user_avatar, "medium", field(user_avatar, "px_170x170", None)),
        "page_count": field(obj, "page_count", None),
        "text_length": field(obj, "text_length", None) if novel else None,
        "language": field(obj, "lang", field(obj, "language", None)) if novel else None,
        "width": field(obj, "width", None) if not novel else None,
        "height": field(obj, "height", None) if not novel else None,
        "tags": [entry["name"] for entry in details],
        "tag_details": details,
        "caption": caption,
        "description": excerpt[:300],
        "description_truncated": len(excerpt) > 300,
        "create_date": field(obj, "create_date", None),
        "thumbnail_url": field(images, "square_medium", field(images, "medium", None)),
        "cover_url": field(images, "medium", None),
        "image_urls": {"square_medium": field(images, "square_medium", None),
                       "medium": field(images, "medium", None),
                       "large": field(images, "large", None)},
        "total_bookmarks": field(obj, "total_bookmarks", None),
        "total_view": field(obj, "total_view", None),
        "total_likes": count_likes,
        "likes_source": ("app_api" if count_likes is not None else "unavailable"),
        "total_comments": field(obj, "total_comments", None),
        "is_bookmarked": field(obj, "is_bookmarked", None),
        "x_restrict": field(obj, "x_restrict", None),
        "ai_type": (field(obj, "novel_ai_type", None) if novel else field(obj, "illust_ai_type", None)),
        "is_original": field(obj, "is_original", None) if novel else None,
        "is_replaceable": field(obj, "is_replaceable", None) if novel else None,
        "series": series_data,
        "url": (f"https://www.pixiv.net/novel/show.php?id={wid}" if novel
                else f"https://www.pixiv.net/artworks/{wid}"),
    }


COMPACT_FIELDS = (
    "id", "kind", "title", "user_id", "user_name", "page_count", "text_length",
    "total_bookmarks", "total_view", "total_likes", "likes_source",
    "create_date", "thumbnail_url", "language", "description", "url",
)


def compact_work(item: dict[str, Any]) -> dict[str, Any]:
    """Small but still useful search result, for MCP context efficiency."""
    return {key: item.get(key) for key in COMPACT_FIELDS}


def positive_int(val: Any, label: str) -> int:
    if isinstance(val, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        n = int(val)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if n <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return n


class PixivClient:
    """Serializes PixivPy API calls and refreshes only on auth failures.

    Calls are guarded to avoid concurrent SDK session races and overloading Pixiv.
    """

    def __init__(self, refresh_token: str | None = None, proxy: str | None = None,
                 delay: float = 0.5, api: Any = None):
        self.refresh_token = (refresh_token if refresh_token is not None else
                              os.getenv("PIXIV_REFRESH_TOKEN", "")).strip()
        self.proxy = (proxy if proxy is not None else os.getenv("PIXIV_HTTPS_PROXY", "")).strip()
        self.delay = max(0.0, float(delay))
        self.api = api
        self.api_profile = os.getenv("PIXIV_APP_API_PROFILE", "pixev").strip().lower()
        self.accept_language = os.getenv("PIXIV_ACCEPT_LANGUAGE", "zh-CN").strip() or "zh-CN"
        self.lock = threading.RLock()
        self.logged_in = False
        self.last_request_at = 0.0

    def _get_api(self) -> Any:
        if self.api is None:
            from pixivpy3 import AppPixivAPI
            kwargs = {"proxies": {"http": self.proxy, "https": self.proxy}} if self.proxy else {}
            compatible_class = make_app_api_class(
                AppPixivAPI, profile=self.api_profile, accept_language=self.accept_language
            )
            self.api = compatible_class(**kwargs)
        return self.api

    def _authenticate(self) -> None:
        if not self.refresh_token:
            raise RuntimeError("PIXIV_REFRESH_TOKEN is missing. Configure it locally in .env.")
        try:
            response = self._get_api().auth(refresh_token=self.refresh_token)
        except Exception as exc:
            # PixivPy may include the POST request body in exceptions: never
            # surface a potentially sensitive refresh token through MCP.
            raise RuntimeError(f"Pixiv authentication failed ({type(exc).__name__}); check token and proxy") from None
        # Pixiv may return a new refresh token; retain it in memory for this process.
        updated = field(field(response, "response", {}), "refresh_token")
        if isinstance(updated, str) and updated:
            self.refresh_token = updated
        self.logged_in = True

    def ensure_auth(self) -> None:
        with self.lock:
            if not self.logged_in:
                self._authenticate()

    def _popular_preview(self, kind: str, word: str, **kwargs: Any) -> dict[str, Any]:
        """Pixev-style non-Premium preview using Pixiv's read-only App API.

        The response represents a LIMITED preview, not the complete Premium
        popular_desc stream. Never follow upstream next_url for preview mode.
        """
        if kind not in {"illust", "manga", "novel"}:
            raise ValueError("Popular preview requires illust, manga or novel")
        api = self._get_api()
        target = "novel" if kind == "novel" else "illust"
        params: dict[str, Any] = {
            "word": word,
            "search_target": kwargs.pop("search_target", "partial_match_for_tags"),
            "filter": "for_android",
            "include_translated_tag_results": self._param_value(kwargs.get("include_translated_tag_results", True)),
            "merge_plain_keyword_results": self._param_value(kwargs.get("merge_plain_keyword_results", True)),
        }
        for key in ("start_date", "end_date"):
            if key in kwargs and kwargs[key] is not None:
                params[key] = kwargs[key]
        # No user-supplied endpoints or hosts. Reuse PixivPy3's OAuth session.
        raw = api.no_auth_requests_call(
            "GET", f"{api.hosts}/v1/search/popular-preview/{target}",
            params=params, req_auth=True,
        )
        parsed = api.parse_result(raw)
        payload = to_dict(parsed)
        key = "novels" if target == "novel" else "illusts"
        entries = field(payload, key, field(payload, f"popular_{key}", None))
        if not isinstance(entries, (tuple, list)):
            raise RuntimeError(f"Pixiv popular preview returned no {key} list")
        return {key: list(entries), "next_url": None}

    @staticmethod
    def _param_value(value: Any) -> Any:
        # Dio/Pixev sends lower-case query booleans, not Python's 'True'/'False'.
        if isinstance(value, bool):
            return "true" if value else "false"
        return value

    _RAW_ONLY_PARAMS = frozenset({
        "bookmark_num_min", "bookmark_num_max", "bookmark_num", "text_length_min",
        "lang", "is_original_only", "is_replaceable_only",
        "include_potential_violation_works", "include_translated_tag_results",
        "merge_plain_keyword_results",
    })

    _SAFE_LOCAL_FALLBACK = frozenset({
        "bookmark_num_min", "bookmark_num_max", "bookmark_num", "text_length_min",
        "is_original_only", "search_ai_type",
    })

    def _search_local_fallback(self, api: Any, method_name: str, word: str,
                               kwargs: dict[str, Any]) -> dict[str, Any]:
        """Only fall back for fields that can be checked against list metadata.

        Pixiv doesn't return reliable novel language metadata in search hits.
        Never silently turn a language filter into an unfiltered search.
        """
        unsupported = self._RAW_ONLY_PARAMS.intersection(kwargs) - self._SAFE_LOCAL_FALLBACK
        if unsupported:
            raise RuntimeError("Pixiv native search unavailable for required API-only options: "
                               + ", ".join(sorted(unsupported)))
        sdk_kwargs = {k: v for k, v in kwargs.items()
                      if k not in self._SAFE_LOCAL_FALLBACK or
                      (k == "search_ai_type" and v == 1)}
        # PixivPy3 doesn't send search_ai_type=0. Always post-filter AI in
        # SearchOptions.matches, and use the SDK's normal search endpoint.
        result = to_dict(getattr(api, method_name)(word, **sdk_kwargs))
        fallback_names: set[str] = set()
        for name in self._SAFE_LOCAL_FALLBACK.intersection(kwargs):
            if name == "search_ai_type" and kwargs[name] == 1:
                continue  # SDK supports nonzero AI setting natively.
            fallback_names.add({
                "bookmark_num": "min_bookmarks",
                "bookmark_num_min": "min_bookmarks",
                "bookmark_num_max": "max_bookmarks",
                "text_length_min": "min_text_length",
                "is_original_only": "originals_only",
                "search_ai_type": "ai_mode",
            }[name])
        result["__mcp_fallback_filters"] = sorted(fallback_names)
        return result

    def _search_native(self, method_name: str, word: str, **kwargs: Any) -> Any:
        """Send Pixev-compatible search options through the authenticated App API.

        PixivPy3 currently omits `search_ai_type=0` and doesn't expose
        bookmark_num_min/max, lang, text_length_min etc in its high-level
        wrappers. Never drop a requested filter because its wrapper lacks it.
        """
        api = self._get_api()
        if (not self._RAW_ONLY_PARAMS.intersection(kwargs) and
            kwargs.get("search_ai_type") != 0):
            # Pure convenience-method search remains compatible with PixivPy3.
            return getattr(api, method_name)(word, **kwargs)
        if not callable(getattr(api, "no_auth_requests_call", None)) or not callable(getattr(api, "parse_result", None)):
            return self._search_local_fallback(api, method_name, word, kwargs)
        target = "novel" if method_name == "search_novel" else "illust"
        params: dict[str, Any] = {
            "word": word,
            "search_target": kwargs.get("search_target", "partial_match_for_tags"),
            "sort": kwargs.get("sort", "date_desc"),
            "filter": "for_android",
        }
        params.update({k: self._param_value(v) for k, v in kwargs.items()
                       if k not in {"search_target", "sort"} and v is not None})
        try:
            response = api.no_auth_requests_call(
                "GET", f"{api.hosts}/v1/search/{target}", params=params, req_auth=True,
            )
            parsed = to_dict(api.parse_result(response))
            if parsed.get("error"):
                diagnostic = inspect_response(response, endpoint=f"/v1/search/{target}",
                                              expected="novels" if target == "novel" else "illusts")
                raise RuntimeError("Pixiv App API reported an error (redacted): " +
                                   json.dumps(diagnostic, ensure_ascii=False))
            item_key = "novels" if target == "novel" else "illusts"
            if not isinstance(parsed.get(item_key), list):
                diagnostic = inspect_response(response, endpoint=f"/v1/search/{target}", expected=item_key)
                raise RuntimeError(f"Pixiv App API response did not include {item_key} (redacted): " +
                                   json.dumps(diagnostic, ensure_ascii=False))
            return parsed
        except Exception as exc:
            # An invalid/unknown-parameter error can permit an accurate local
            # post-filter. Do not downgrade for timeouts, authentication, 429,
            # or arbitrary server failures. Never drop lang or other API-only
            # filters, even if the server rejects them.
            description = str(exc).lower()
            if any(x in description for x in (
                "unknown parameter", "unsupported parameter", "invalid parameter",
                "unexpected keyword", "invalid query parameter", "unrecognized parameter",
            )) or (re.search(r"\b(?:400|403)\b", description) is not None and
                    not any(x in description for x in (
                        "oauth", "invalid_grant", "token", "authorization", "unauthorized",
                    ))):
                # A 400/403 may mean the API rejected a Premium-only filter.
                # Only retry if every extra parameter has a faithful local
                # equivalent. A retry failure is still surfaced to the caller.
                return self._search_local_fallback(api, method_name, word, kwargs)
            raise
        # PixivPy3 normalizes API errors when parsing the response. Propagate
        # failures, instead of silently dropping unsupported server filters.

    def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            self.ensure_auth()
            for attempt in range(2):
                elapsed = time.monotonic() - self.last_request_at
                if elapsed < self.delay:
                    time.sleep(self.delay - elapsed)
                self.last_request_at = time.monotonic()
                try:
                    if method_name == "_popular_preview":
                        return self._popular_preview(*args, **kwargs)
                    if method_name in {"search_illust", "search_novel"}:
                        return self._search_native(method_name, *args, **kwargs)
                    return getattr(self._get_api(), method_name)(*args, **kwargs)
                except Exception as exc:
                    msg = str(exc).lower()
                    auth_failure = any(k in msg for k in
                                       ("invalid_grant", "invalid_token", "unauthorized", "oauth", "401"))
                    if attempt or not auth_failure:
                        raise
                    LOGGER.warning("Pixiv access token may have expired; retrying OAuth authentication")
                    self.logged_in = False
                    self._authenticate()
            raise RuntimeError("Unexpected retry state")

    def diagnose_app_api(self, *, illust_id: int | None = None,
                         novel_id: int | None = None, word: str = "風景") -> dict[str, Any]:
        """Read-only, redacted probe using the same session as normal MCP calls.

        Never returns response bodies, token, cookies, arbitrary remote URLs,
        user metadata or raw headers.
        """
        with self.lock:
            self.ensure_auth()
            api = self._get_api()
            if not callable(getattr(api, "no_auth_requests_call", None)):
                raise RuntimeError("Active SDK does not expose a raw App API request method")
            queries = [
                ("/v1/search/illust", {"word": word, "search_target": "partial_match_for_tags", "filter": "for_android"}, "illusts"),
                ("/v1/user/illusts", {"user_id": self.user_id, "filter": "for_android"}, "illusts"),
            ]
            if illust_id is not None:
                queries.append(("/v1/illust/detail", {"illust_id": positive_int(illust_id, "illust_id")}, "illust"))
            if novel_id is not None:
                queries.append(("/webview/v2/novel", {"id": positive_int(novel_id, "novel_id")}, None))
            output = []
            for path, params, expected in queries:
                try:
                    response = api.no_auth_requests_call("GET", f"{api.hosts}{path}", params=params, req_auth=True)
                    output.append(inspect_response(response, endpoint=path, expected=expected))
                except Exception as exc:
                    output.append({"path": path, "exception_type": type(exc).__name__})
            return {
                "api_profile": self.api_profile,
                "transport": "PixivPy3/cloudscraper (not Pixev/rhttp)",
                "authenticated": self.logged_in,
                "checks": output,
                "interpretation": "Keys/status are diagnostic evidence, not proof that a backend is unblocked.",
            }

    @property
    def user_id(self) -> int:
        self.ensure_auth()
        return positive_int(field(self._get_api(), "user_id"), "authenticated user id")


class PixivEngine:
    """Fetch bounded work lists by IDs, artist, bookmarks, search or novel series."""

    def __init__(self, client: PixivClient):
        self.client = client
        self._sort_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._sort_lock = threading.RLock()

    def _collect(self, method: str, item_key: str, params: dict[str, Any],
                 limit: int, item_filter: Callable[[Any], bool] | None = None) -> list[Any]:
        result: list[Any] = []
        seen_ids: set[int] = set()
        seen_pages: set[str] = set()
        kw = dict(params)
        for _ in range(100):
            if len(result) >= limit:
                break
            page = self.client.call(method, **kw)
            entries = field(page, item_key)
            if entries is None:
                diagnostic = getattr(self.client._get_api(), "last_response_diagnostic", None)
                raise RuntimeError(f"Pixiv {method} response did not include {item_key}; "
                                   f"response_shape={json.dumps(diagnostic, ensure_ascii=False)}")
            if not isinstance(entries, (list, tuple)):
                raise ValueError(f"Unexpected Pixiv response: missing {item_key}")
            for obj in entries:
                if item_filter and not item_filter(obj):
                    continue
                wid = positive_int(field(obj, "id"), "work id")
                if wid in seen_ids:
                    continue
                result.append(obj)
                seen_ids.add(wid)
                if len(result) >= limit:
                    break
            next_url = field(page, "next_url")
            if not next_url or next_url in seen_pages:
                break
            seen_pages.add(next_url)
            q = parse_qs(urlparse(next_url).query)
            # Do not allow arbitrary server-provided URLs to become request targets.
            # Use only pagination parameters with SDK methods.
            cursor = {k: v[-1] for k, v in q.items() if k in {"offset", "last_order", "max_bookmark_id"}}
            if not cursor:
                break
            if all(str(kw.get(k)) == str(v) for k, v in cursor.items()):
                break
            kw.update(cursor)
        return result

    def list_works(self, *, source: str, kind: str, limit: int = 30,
                   user_id: int | None = None, query: str | None = None,
                   series_id: int | None = None, restrict: str = "public",
                   options: SearchOptions | None = None) -> list[dict[str, Any]]:
        options = options or SearchOptions()
        if source not in {"user", "bookmarks", "search", "series"}:
            raise ValueError("source must be user, bookmarks, search or series")
        if kind not in {"illust", "manga", "novel", "all"}:
            raise ValueError("kind must be illust, manga, novel or all")
        if restrict not in {"public", "private"}:
            raise ValueError("restrict must be public or private")
        if options is not None and source != "search":
            if (options.lang is not None or options.is_replaceable_only is not None or
                    options.include_potential_violation_works is not None or
                    options.include_translated_tag_results is not None or
                    options.merge_plain_keyword_results is not None or
                options.include_likes or
                    options.search_target != "partial_match_for_tags" or
                    options.sort in ((UPSTREAM_SORTS - {"date_desc"}) | PREVIEW_SORTS)):
                raise ValueError("Native search-only options/likes cannot be reliably applied to "
                                 "user/bookmarks/series lists; use source=search")
        limit = positive_int(limit, "limit")
        if source != "series" and options is not None:
            options.validate_kind(kind)
        if limit > 1000:
            raise ValueError("limit must be <= 1000")
        if kind == "all" and source != "series":
            # Reserve space for each content type; otherwise a prolific illustrator
            # might consume the entire cap before any novels are considered.
            quotas = ((limit + 2) // 3, (limit + 1) // 3, limit // 3)
            combined: list[dict[str, Any]] = []
            for subkind, quota in zip(("illust", "manga", "novel"), quotas):
                if quota:
                    combined.extend(self.list_works(source=source, kind=subkind,
                        limit=quota, user_id=user_id, query=query,
                        series_id=series_id, restrict=restrict, options=options))
            return combined
        if source == "search":
            # This path intentionally reuses the same search/filter semantics as
            # pixiv_search_advanced and bulk download. Pagination preserves any
            # partial Pixiv API page, even after a local metadata filter.
            options.validate_kind(kind)
            items: list[dict[str, Any]] = []
            cursor: str | None = None
            scanned = 0
            budget = max(200, min(1000, limit * 10))
            for _ in range(40):
                remaining_scan = budget - scanned
                if len(items) >= limit:
                    break
                # A local-sort page may already have scanned the whole allowed
                # sample but still have cached results to page through. Do not
                # truncate bulk downloads at the first 50 sorted works.
                continuing_cache = (bool(cursor) and options.local_sort and
                                    decode_cursor(cursor).get("mode") == "sorted_cache")
                if remaining_scan <= 0 and not continuing_cache:
                    break
                response = self.search_advanced(
                    query=query or "", kind=kind,
                    page_size=min(20 if options.include_likes else 50, limit - len(items)),
                    cursor=cursor, max_scan=max(1, min(remaining_scan, 500)), options=options,
                )
                items.extend(response["items"])
                scanned += response["scanned_count"]
                cursor = response["next_cursor"]
                if not cursor or response["scanned_count"] == 0:
                    break
            return options.sort_items(items)[:limit]
        if source == "series":
            if kind not in {"novel", "all"}:
                raise ValueError("series currently supports novels only")
            series_id = positive_int(series_id, "series_id")
            pages = self._collect("novel_series", "novels", {"series_id": series_id},
                                  limit, lambda p: options.matches(work_summary(p, "novel")))
            return options.sort_items([work_summary(p, "novel") for p in pages])
        if source in {"user", "bookmarks"}:
            user_id = positive_int(user_id if user_id is not None else self.client.user_id, "user_id")
        if source == "search" and not query:
            raise ValueError("query required for search")
        found: list[dict[str, Any]] = []
        if kind in {"illust", "manga", "all"}:
            if source == "user":
                for typ in (["illust", "manga"] if kind == "all" else [kind]):
                    entries = self._collect("user_illusts", "illusts", {"user_id": user_id, "type": typ},
                                            limit - len(found), lambda o, t=typ: (
                                                (field(o, "type", t) == t or
                                                 (t == "illust" and field(o, "type") == "ugoira"))
                                                and options.matches(work_summary(o, t))))
                    found.extend(work_summary(e, typ) for e in entries)
                    if len(found) >= limit:
                        break
            elif source == "bookmarks":
                entries = self._collect("user_bookmarks_illust", "illusts", {"user_id": user_id, "restrict": restrict},
                                        limit, lambda o: (
                                            (kind == "all" or field(o, "type") == kind or
                                             (kind == "illust" and field(o, "type") == "ugoira"))
                                            and options.matches(work_summary(o, "illust"))))
                found.extend(work_summary(e, "illust") for e in entries)
            else:
                entries = self._collect("search_illust", "illusts", {"word": query}, limit,
                                        lambda o: kind == "all" or field(o, "type") == kind or (kind == "illust" and field(o, "type") == "ugoira"))
                found.extend(work_summary(e, "illust") for e in entries)
        if kind in {"novel", "all"} and len(found) < limit:
            if source == "user":
                method, params = "user_novels", {"user_id": user_id}
            elif source == "bookmarks":
                method, params = "user_bookmarks_novel", {"user_id": user_id, "restrict": restrict}
            else:
                method, params = "search_novel", {"word": query}
            entries = self._collect(method, "novels", params, limit - len(found),
                                    lambda o: options.matches(work_summary(o, "novel")))
            found.extend(work_summary(e, "novel") for e in entries)
        # IDs for illustration/manga share a namespace; type is unique per ID.
        dedup: dict[tuple[str, int], dict[str, Any]] = {}
        for item in found:
            cls = "novel" if item["kind"] == "novel" else "artwork"
            dedup[(cls, item["id"])] = item
        return options.sort_items([w for w in dedup.values() if options.matches(w)])[:limit]

    def _get_likes(self, work: dict[str, Any]) -> None:
        """Opt-in extra request for likes (not present in illustration App API).

        Public Pixiv AJAX can reject requests depending on network, region and
        login status. Never silently report zero when this happens.
        """
        if work.get("total_likes") is not None:
            return
        if work["kind"] == "novel":
            try:
                web = self.novel_webview(work["id"])
                likes = field(field(web, "rating", {}), "like")
                if isinstance(likes, int) and not isinstance(likes, bool):
                    work["total_likes"] = likes
                    work["likes_source"] = "novel_webview"
            except Exception as exc:
                LOGGER.info("Novel like count unavailable for %d (%s)", work["id"], type(exc).__name__)
        else:
            try:
                url = f"https://www.pixiv.net/ajax/illust/{positive_int(work['id'], 'id')}"
                proxies = {"https": self.client.proxy} if self.client.proxy else None
                response = requests.get(url, timeout=(6, 12), allow_redirects=False,
                                        headers={"Referer": "https://www.pixiv.net/", "User-Agent": "Mozilla/5.0"},
                                        proxies=proxies)
                if response.status_code != 200 or "json" not in response.headers.get("Content-Type", ""):
                    return
                data = response.json()
                if not isinstance(data, dict) or data.get("error"):
                    return
                likes = field(field(data, "body", {}), "likeCount")
                if isinstance(likes, int) and not isinstance(likes, bool):
                    work["total_likes"] = likes
                    work["likes_source"] = "public_web_ajax"
            except (requests.RequestException, ValueError, TypeError) as exc:
                LOGGER.info("Artwork like count unavailable for %d (%s)", work["id"], type(exc).__name__)

    def search_advanced(self, *, query: str, kind: str = "illust", page_size: int = 20,
                        cursor: str | None = None, max_scan: int = 200,
                        options: SearchOptions | None = None, view: str = "full") -> dict[str, Any]:
        """A bounded Pixiv search page with stable resume even mid-upstream-page.

        The options signature binds the cursor to its original query/filter set.
        Local filters are applied only to up to max_scan upstream results, and
        local sorting only applies within this returned window, never globally.
        """
        options = options or SearchOptions()
        options.validate_kind(kind)
        if view not in {"full", "compact"}:
            raise ValueError("view must be full or compact")
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("query required (1..500 chars)")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 50:
            raise ValueError("page_size must be 1..50")
        if isinstance(max_scan, bool) or not isinstance(max_scan, int) or not 1 <= max_scan <= 1000:
            raise ValueError("max_scan must be 1..1000")
        if options.include_likes and page_size > 20:
            raise ValueError("When include_likes=true, page_size must be <=20 to limit extra requests")
        if kind == "all" and (page_size < 3 or max_scan < 3):
            raise ValueError("kind=all requires page_size>=3 and max_scan>=3 to cover all three categories")
        signature = options.signature(query, kind)
        if options.local_sort and kind != "all":
            return self._search_locally_sorted(
                query=query, kind=kind, page_size=page_size, cursor=cursor,
                max_scan=max_scan, options=options, signature=signature, view=view)
        if kind == "all":
            if cursor:
                token = decode_cursor(cursor)
                if token.get("signature") != signature or token.get("kind") != "all":
                    raise ValueError("Cursor does not match this search query/filters")
                subcursors = token.get("parts")
                if not isinstance(subcursors, dict) or set(subcursors) != {"illust", "manga", "novel"}:
                    raise ValueError("Invalid multi-kind cursor")
            else:
                subcursors = {"illust": "", "manga": "", "novel": ""}
            quotas = ((page_size + 2) // 3, (page_size + 1) // 3, page_size // 3)
            items: list[dict[str, Any]] = []
            scanned = 0
            new_parts: dict[str, str | None] = {}
            per_type_execution: dict[str, Any] = {}
            for subkind, quota in zip(("illust", "manga", "novel"), quotas):
                child = subcursors[subkind]
                if child is None:
                    new_parts[subkind] = None
                    continue
                if quota == 0:
                    new_parts[subkind] = child
                    continue
                response = self.search_advanced(query=query, kind=subkind, page_size=quota,
                    cursor=child or None, max_scan=max(1, max_scan // 3), options=options,
                    view="full")
                items.extend(response["items"])
                scanned += response["scanned_count"]
                new_parts[subkind] = response["next_cursor"]
                per_type_execution[subkind] = response.get("filter_execution", options.filter_plan(subkind))
            next_cursor = (encode_cursor({"v": 2, "kind": "all", "signature": signature,
                                           "parts": new_parts}) if any(v is not None for v in new_parts.values()) else None)
            items = options.sort_items(items)
            if view == "compact":
                items = [compact_work(item) for item in items]
            return {"query": query, "kind": kind, "view": view, "items": items,
                    "count": len(items), "scanned_count": scanned, "has_more": next_cursor is not None,
                    "next_cursor": next_cursor,
                    "filter_execution": {"per_type": per_type_execution},
                    "sort_scope": ("scanned_window" if options.local_sort else "per_type_pixiv_order"),
                    "note": ("kind=all uses separate Pixiv searches for each type. "
                             "Premium popular_desc ranks EACH type, not a global interleaved top list. "
                             "The mixed result may contain fewer works than page_size.")}

        method = ("_popular_preview" if options.sort == "popular_preview" else
                  "search_novel" if kind == "novel" else "search_illust")
        item_key = "novels" if kind == "novel" else "illusts"
        request_offset: str | None = None
        index = 0
        if cursor:
            token = decode_cursor(cursor)
            if token.get("signature") != signature or token.get("kind") != kind:
                raise ValueError("Cursor does not match this search query/filters")
            request_offset = token.get("offset")
            index = token.get("index")
            if request_offset is not None and (not isinstance(request_offset, str) or len(request_offset) > 200):
                raise ValueError("Invalid cursor offset")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 1000:
                raise ValueError("Invalid cursor page position")
        upstream_params = options.api_kwargs(kind)
        seen_offsets: set[str | None] = set()
        matched: list[dict[str, Any]] = []
        actual_fallbacks: set[str] = set()
        scanned = 0
        resume: tuple[str | None, int] | None = None
        for _ in range(50):
            if request_offset in seen_offsets:
                break
            seen_offsets.add(request_offset)
            args = dict(upstream_params)
            if request_offset is not None:
                args["offset"] = request_offset
            # Prefer caller's provided query as positional argument to work with
            # PixivPy3 as well as test doubles.
            response = (self.client.call(method, kind, query, **args)
                        if options.sort == "popular_preview" else
                        self.client.call(method, query, **args))
            actual_fallbacks.update(field(response, "__mcp_fallback_filters", ()) or ())
            entries = field(response, item_key)
            if entries is None:
                diagnostic = getattr(self.client._get_api(), "last_response_diagnostic", None)
                raise RuntimeError(f"Pixiv {method} response did not include {item_key}; "
                                   f"response_shape={json.dumps(diagnostic, ensure_ascii=False)}")
            if not isinstance(entries, (tuple, list)):
                raise RuntimeError(f"Pixiv response missing {item_key}")
            if index > len(entries):
                raise ValueError("Cursor points beyond the upstream page")
            position = index
            while position < len(entries):
                obj = entries[position]
                position += 1
                scanned += 1
                actual = "novel" if kind == "novel" else field(obj, "type", "illust")
                if actual == kind or (kind == "illust" and actual in {"illust", "ugoira"}):
                    result = work_summary(obj, kind)
                    if options.matches(result):
                        matched.append(result)
                if scanned >= max_scan or len(matched) >= page_size:
                    break
            if position < len(entries):
                resume = (request_offset, position)
                break
            next_url = field(response, "next_url")
            if not next_url:
                resume = None
                break
            q = parse_qs(urlparse(next_url).query)
            new_offset = (q.get("offset") or [None])[-1]
            if new_offset is None or new_offset == request_offset or len(new_offset) > 200:
                resume = None
                break
            resume = (new_offset, 0)
            if len(matched) >= page_size or scanned >= max_scan:
                break
            request_offset, index = new_offset, 0
        if resume is not None:
            next_token = encode_cursor({"v": 2, "signature": signature, "kind": kind,
                                        "offset": resume[0], "index": resume[1]})
        else:
            next_token = None
        if options.include_likes:
            for item in matched:
                self._get_likes(item)
        items = options.sort_items(matched)
        if view == "compact":
            items = [compact_work(item) for item in items]
        sort_scope = ("limited_popular_preview" if options.sort == "popular_preview" else
                      "scanned_window" if options.local_sort else "pixiv_order")
        note = ("Pixiv non-Premium popular preview is limited; no further upstream pages. "
                "The next_cursor only walks items within the returned preview."
                if options.sort == "popular_preview" else
                "Local filtering scans a bounded window, and bookmarks/views sorting only sorts that window"
                if options.local_sort else "Pixiv applied server-side sorting; local filters only inspect scanned API results")
        filter_execution = options.filter_plan(kind)
        filter_execution["fallback_to_local"] = sorted(actual_fallbacks)
        if actual_fallbacks:
            filter_execution["native_requested"] = [n for n in filter_execution["native_requested"]
                                                     if n not in {"bookmark_num_min" if "min_bookmarks" in actual_fallbacks else "",
                                                                  "bookmark_num_max" if "max_bookmarks" in actual_fallbacks else "",
                                                                  "bookmark_num" if "min_bookmarks" in actual_fallbacks else "",
                                                                  "text_length_min" if "min_text_length" in actual_fallbacks else "",
                                                                  "search_ai_type" if "ai_mode" in actual_fallbacks else "",
                                                                  "is_original_only" if "originals_only" in actual_fallbacks else ""}]
            filter_execution["local_post_filter"] = list(dict.fromkeys(
                filter_execution["local_post_filter"] + sorted(actual_fallbacks)))
        return {"query": query, "kind": kind, "view": view, "items": items, "count": len(items),
                "scanned_count": scanned, "has_more": next_token is not None,
                "next_cursor": next_token, "sort_scope": sort_scope, "note": note,
                "filter_execution": filter_execution}

    def _search_locally_sorted(self, *, query: str, kind: str, page_size: int,
                               cursor: str | None, max_scan: int,
                               options: SearchOptions, signature: str,
                               view: str) -> dict[str, Any]:
        """Sort a bounded sample, then page through it without dropping works.

        Local sorts never claim to be global. A new sample is fetched after the
        cached sample is exhausted. Cache entries expire to bound memory; if a
        cursor refers to expired state it explicitly errors instead of skipping.
        """
        def result_page(entry: dict[str, Any], position: int, scanned: int) -> dict[str, Any]:
            all_items: list[dict[str, Any]] = entry["items"]
            end = min(len(all_items), position + page_size)
            selected = all_items[position:end]
            if options.include_likes:
                for item in selected:
                    self._get_likes(item)
            if end < len(all_items):
                next_cursor = encode_cursor({"v": 2, "signature": signature, "kind": kind,
                                             "mode": "sorted_cache", "cache_id": entry["id"], "index": end})
            elif entry["upstream_cursor"]:
                next_cursor = encode_cursor({"v": 2, "signature": signature, "kind": kind,
                                             "mode": "sorted_next", "source_cursor": entry["upstream_cursor"]})
            else:
                next_cursor = None
            return {"query": query, "kind": kind, "view": view,
                    "items": [compact_work(item) for item in selected] if view == "compact" else selected,
                    "count": len(selected), "scanned_count": scanned,
                    "sampled_matches": len(all_items),
                    "has_more": next_cursor is not None, "next_cursor": next_cursor,
                    "sort_scope": "scanned_window",
                    "filter_execution": entry.get("filter_execution", options.filter_plan(kind)),
                    "note": ("Sorted only among this window of upstream works, not globally. "
                             "Cursor cache expires after 15 minutes or MCP restart.")}

        source_cursor: str | None = None
        with self._sort_lock:
            now = time.monotonic()
            for key, value in list(self._sort_cache.items()):
                if now - value["created"] > 900:
                    self._sort_cache.pop(key, None)
            if cursor:
                state = decode_cursor(cursor)
                if state.get("signature") != signature or state.get("kind") != kind:
                    raise ValueError("Cursor does not match this search query/filters")
                if state.get("mode") == "sorted_cache":
                    entry = self._sort_cache.get(state.get("cache_id"))
                    index = state.get("index")
                    if entry is None:
                        raise ValueError("Local sort cursor expired; restart this search")
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(entry["items"]):
                        raise ValueError("Invalid local sort cursor")
                    self._sort_cache.move_to_end(entry["id"])
                    return result_page(entry, index, 0)
                if state.get("mode") != "sorted_next":
                    raise ValueError("Invalid local sort cursor")
                source_cursor = state.get("source_cursor")
                if not isinstance(source_cursor, str) or len(source_cursor) > 4000:
                    raise ValueError("Invalid local sort source cursor")

        # Fetch only the configured upstream sample (not arbitrary Pixiv pages).
        # search_advanced() uses its normal opaque cursor for each unsorted page.
        unsorted_options = replace(options, sort="date_desc", include_likes=False)
        scanned = 0
        collected: list[dict[str, Any]] = []
        filter_execution = options.filter_plan(kind)
        all_fallbacks: set[str] = set()
        for _ in range(50):
            if scanned >= max_scan:
                break
            response = self.search_advanced(query=query, kind=kind, view="full",
                page_size=min(50, max_scan - scanned), cursor=source_cursor,
                max_scan=max_scan - scanned, options=unsorted_options)
            collected.extend(response["items"])
            if response.get("filter_execution"):
                filter_execution = response["filter_execution"]
                all_fallbacks.update(filter_execution.get("fallback_to_local", ()))
            scanned += response["scanned_count"]
            source_cursor = response["next_cursor"]
            if not source_cursor or response["scanned_count"] == 0:
                break
        filter_execution["fallback_to_local"] = sorted(all_fallbacks)
        filter_execution["local_post_filter"] = list(dict.fromkeys(
            filter_execution.get("local_post_filter", []) + sorted(all_fallbacks)))
        entry = {"id": uuid.uuid4().hex[:24], "items": options.sort_items(collected),
                 "created": time.monotonic(), "upstream_cursor": source_cursor,
                 "filter_execution": filter_execution}
        with self._sort_lock:
            self._sort_cache[entry["id"]] = entry
            while len(self._sort_cache) > 16:
                self._sort_cache.popitem(last=False)
        return result_page(entry, 0, scanned)

    def get_work(self, id: int, kind: str) -> dict[str, Any]:
        id = positive_int(id, "id")
        if kind == "novel":
            detail = self.client.call("novel_detail", id)
            return work_summary(detail, "novel")
        if kind in {"artwork", "illust", "manga"}:
            detail = self.client.call("illust_detail", id)
            illust = field(detail, "illust", detail)
            return work_summary(illust, "illust")
        raise ValueError("kind must be novel or artwork")

    def read_novel(self, id: int, offset: int = 0, max_chars: int = 12000) -> dict[str, Any]:
        id = positive_int(id, "id")
        if offset < 0 or not (1 <= max_chars <= 40000):
            raise ValueError("offset must be >=0; max_chars 1..40000")
        result = self.novel_webview(id)
        text = field(result, "text", None)
        if not isinstance(text, str):
            raise RuntimeError("Pixiv returned no novel text")
        title = field(result, "title", "")
        return {"id": id, "title": title, "offset": offset, "total_chars": len(text),
                "text": text[offset:offset + max_chars],
                "has_more": offset + max_chars < len(text)}

    def novel_webview(self, novel_id: int) -> Any:
        """Read the novel; fall back to PixEz-style balanced script JSON parsing.

        PixivPy3's normal parser uses a regex that can fail when webview HTML
        formatting changes. The raw endpoint is exactly the one PixEz uses.
        """
        novel_id = positive_int(novel_id, "novel_id")
        try:
            return self.client.call("webview_novel", novel_id)
        except Exception as exc:
            if "Extract novel content error" not in str(exc) and "extract novel content" not in str(exc).lower():
                raise
            LOGGER.warning("PixivPy3 novel extraction failed; trying balanced PixEz-style parsing")
            raw = self.client.call("webview_novel", novel_id, raw=True)
            try:
                return parse_novel_webview_html(raw)
            except (ValueError, TypeError) as parse_exc:
                diagnostic = getattr(self.client._get_api(), "last_response_diagnostic", None)
                raise RuntimeError(
                    "Pixiv novel webview response could not be parsed; "
                    f"response_shape={json.dumps(diagnostic, ensure_ascii=False)}; "
                    f"parser={type(parse_exc).__name__}"
                ) from None


def parse_novel_webview_html(html: str) -> dict[str, Any]:
    """Read the JSON literal after a `novel:` key in a Pixiv webview script.

    Uses the standard JSON decoder to match balanced braces including quotes,
    instead of stopping at the first closing brace or greedy regex capture.
    """
    if not isinstance(html, str):
        raise ValueError("Pixiv webview did not return HTML")
    for script in re.finditer(r"<script\b[^>]*>(.*?)</script\s*>", html, re.I | re.S):
        content = script.group(1)
        marker = re.search(r"\bnovel\s*:", content)
        if marker is None:
            continue
        object_start = content.find("{", marker.end())
        if object_start < 0:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(content[object_start:])
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                return data
        except (json.JSONDecodeError, ValueError):
            continue
    raise ValueError("Unable to extract novel JSON from Pixiv webview HTML")
