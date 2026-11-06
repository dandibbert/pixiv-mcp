"""API-first Pixiv search and bounded local post-filtering.

Pixiv supports Premium popular_desc for BOTH novels and artwork; non-Premium
popular_preview is a separate, intentionally limited App API endpoint. Local
bookmarks_desc/views_desc are NOT Pixiv-wide popularity rankings.

Some Pixiv App API parameters exposed by Pixev are absent from the PixivPy3
convenience methods; the adapter sends those through PixivPy3's authenticated
raw request method instead of silently discarding them.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any

SEARCH_TARGETS = {
    "illust": {"partial_match_for_tags", "exact_match_for_tags", "title_and_caption"},
    "manga": {"partial_match_for_tags", "exact_match_for_tags", "title_and_caption"},
    "novel": {"partial_match_for_tags", "exact_match_for_tags", "text", "keyword"},
}
UPSTREAM_SORTS = {"date_desc", "date_asc", "popular_desc", "popular_male_desc", "popular_female_desc"}
PREVIEW_SORTS = {"popular_preview"}
LOCAL_SORTS = {"bookmarks_desc", "views_desc"}
DURATIONS = {"within_last_day", "within_last_week", "within_last_month"}
LANG_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}$")


def _lang(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("lang must be a language code such as ja or zh-CN")
    value = value.strip().replace("_", "-")
    value = value[:2].lower() + value[2:] if len(value) >= 2 else value
    if not LANG_PATTERN.fullmatch(value):
        raise ValueError("lang must be a language code such as ja or zh-CN")
    parts = value.split("-")
    parts[0] = parts[0].lower()
    if len(parts) > 1 and len(parts[1]) == 2:
        parts[1] = parts[1].upper()
    return "-".join(parts)


def _date(value: str | None, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid YYYY-MM-DD") from exc


def _optional_nonnegative(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _tags(value: list[str] | None, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > 30:
        raise ValueError(f"{label} must be a list of at most 30 tags")
    if not all(isinstance(v, str) and v.strip() and len(v) <= 100 for v in value):
        raise ValueError(f"{label} contains an invalid tag")
    return tuple(dict.fromkeys(v.strip() for v in value))


@dataclass(frozen=True)
class SearchOptions:
    search_target: str = "partial_match_for_tags"
    sort: str = "date_desc"
    start_date: str | None = None
    end_date: str | None = None
    duration: str | None = None
    min_bookmarks: int | None = None
    max_bookmarks: int | None = None
    min_views: int | None = None
    max_views: int | None = None
    min_pages: int | None = None
    max_pages: int | None = None
    min_text_length: int | None = None
    max_text_length: int | None = None
    lang: str | None = None
    rating: str = "all"  # all, safe, r18, r18g
    ai_mode: str = "any"  # any, exclude, only
    bookmarked: bool | None = None
    include_tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    originals_only: bool | None = None
    is_replaceable_only: bool | None = None
    include_potential_violation_works: bool | None = None
    include_translated_tag_results: bool | None = None
    merge_plain_keyword_results: bool | None = None
    include_likes: bool = False

    @classmethod
    def create(cls, **kwargs: Any) -> "SearchOptions":
        data = dict(kwargs)
        # Accept the more discoverable name in JSON filter dictionaries, too.
        if "language" in data:
            if data.get("lang") not in (None, data["language"]):
                raise ValueError("lang and language disagree")
            data["lang"] = data.pop("language")
        data["lang"] = _lang(data.get("lang"))
        data["start_date"] = _date(data.get("start_date"), "start_date")
        data["end_date"] = _date(data.get("end_date"), "end_date")
        if data["start_date"] and data["end_date"] and data["start_date"] > data["end_date"]:
            raise ValueError("start_date cannot be after end_date")
        for label in ("min_bookmarks", "max_bookmarks", "min_views", "max_views", "min_pages",
                      "max_pages", "min_text_length", "max_text_length"):
            data[label] = _optional_nonnegative(data.get(label), label)
        for name in ("bookmarks", "views", "pages", "text_length"):
            a, b = data.get(f"min_{name}"), data.get(f"max_{name}")
            if a is not None and b is not None and a > b:
                raise ValueError(f"min_{name} cannot exceed max_{name}")
        data["include_tags"] = _tags(data.get("include_tags"), "include_tags")
        data["exclude_tags"] = _tags(data.get("exclude_tags"), "exclude_tags")
        for label in ("bookmarked", "originals_only", "include_likes",
                      "is_replaceable_only", "include_potential_violation_works",
                      "include_translated_tag_results", "merge_plain_keyword_results"):
            if data.get(label) is not None and not isinstance(data[label], bool):
                raise ValueError(f"{label} must be true, false or null")
        value = cls(**data)
        if value.search_target not in set.union(*SEARCH_TARGETS.values()):
            raise ValueError("Unsupported search_target")
        if value.sort not in UPSTREAM_SORTS | PREVIEW_SORTS | LOCAL_SORTS:
            raise ValueError("Unsupported sort")
        if value.duration is not None and value.duration not in DURATIONS:
            raise ValueError("Unsupported duration")
        if value.rating not in {"all", "safe", "r18", "r18g"}:
            raise ValueError("rating must be all, safe, r18, r18g")
        if value.ai_mode not in {"any", "exclude", "only"}:
            raise ValueError("ai_mode must be any, exclude, only")
        return value

    def validate_kind(self, kind: str) -> None:
        if kind not in {"illust", "manga", "novel", "all"}:
            raise ValueError("kind must be illust, manga, novel, all")
        if kind == "all":
            if self.search_target not in SEARCH_TARGETS["novel"] & SEARCH_TARGETS["illust"]:
                raise ValueError("For kind=all use a shared tag search target")
            if self.sort in {"popular_preview", "popular_male_desc", "popular_female_desc"} or self.duration is not None:
                raise ValueError("kind=all supports Premium popular_desc per content type, but not popular_preview, gender sorts or duration")
            if (self.min_text_length is not None or self.max_text_length is not None or
                self.originals_only is not None or self.lang is not None or
                self.is_replaceable_only is not None or
                self.include_potential_violation_works is not None):
                raise ValueError("Novel-only filters require kind=novel")
            return
        if self.search_target not in SEARCH_TARGETS[kind]:
            raise ValueError(f"{self.search_target} is unsupported for kind={kind}")
        if kind == "novel":
            if self.sort in {"popular_male_desc", "popular_female_desc"}:
                raise ValueError("Novel search does not support gender-specific popular sorting")
            if self.duration is not None:
                raise ValueError("Pixiv novel search API has no duration filter; use start_date/end_date")
            if self.sort == "popular_preview" and (self.lang is not None or
                    self.is_replaceable_only is not None or
                    self.include_potential_violation_works is not None):
                raise ValueError("The limited popular_preview endpoint cannot guarantee novel language/"
                                 "replaceable/violation filters; use sort=popular_desc or date_desc")
        elif (self.min_text_length is not None or self.max_text_length is not None or
              self.originals_only is not None or self.lang is not None or
              self.is_replaceable_only is not None or
              self.include_potential_violation_works is not None):
            raise ValueError("Novel language/text-length/original filters require kind=novel")

    @property
    def local_sort(self) -> bool:
        return self.sort in LOCAL_SORTS

    def api_kwargs(self, kind: str) -> dict[str, Any]:
        self.validate_kind(kind)
        result: dict[str, Any] = {}
        # Omit default values: PixivPy3 supplies them, and old clients/tests
        # with narrow method signatures remain compatible.
        if self.search_target != "partial_match_for_tags":
            result["search_target"] = self.search_target
        if self.sort not in {"date_desc", "bookmarks_desc", "views_desc", "popular_preview"}:
            result["sort"] = self.sort
        for key in ("start_date", "end_date"):
            if getattr(self, key) is not None:
                result[key] = getattr(self, key)
        if kind != "novel" and self.duration is not None:
            result["duration"] = self.duration
        # These filters are natively supported by Pixiv App API (Pixev source),
        # but not all of them appear in the PixivPy3 search convenience methods.
        if self.min_bookmarks is not None and self.min_bookmarks > 0:
            result["bookmark_num_min"] = self.min_bookmarks
            if kind == "novel":
                result["bookmark_num"] = self.min_bookmarks  # Pixev compatibility
        if self.max_bookmarks is not None and self.max_bookmarks > 0:
            result["bookmark_num_max"] = self.max_bookmarks
        if self.ai_mode == "exclude":
            # PixivPy3's `if search_ai_type:` drops zero. Client MUST use raw API.
            result["search_ai_type"] = 0
        else:
            # 1 includes AI. `any` must not unintentionally inherit Pixiv's
            # sometimes restrictive default; `only` additionally post-filters.
            result["search_ai_type"] = 1
        if self.merge_plain_keyword_results is not None:
            result["merge_plain_keyword_results"] = self.merge_plain_keyword_results
        if self.include_translated_tag_results is not None:
            result["include_translated_tag_results"] = self.include_translated_tag_results
        if kind == "novel":
            if self.lang is not None:
                result["lang"] = self.lang
            if self.min_text_length is not None and self.min_text_length > 0:
                result["text_length_min"] = self.min_text_length
            if self.originals_only is True:
                result["is_original_only"] = True
            if self.is_replaceable_only is True:
                result["is_replaceable_only"] = True
            if self.include_potential_violation_works is not None:
                result["include_potential_violation_works"] = self.include_potential_violation_works
        return result

    def filter_plan(self, kind: str, *, source: str = "search") -> dict[str, Any]:
        """Explain which user filters are delegated to the server and which are local.

        'native' means requested from Pixiv, not that Pixiv confirmed honoring
        the parameter. A real Pixiv integration test is needed for that.
        """
        api = self.api_kwargs(kind) if source == "search" else {}
        if self.sort == "popular_preview":
            # Pixiv's one-page preview is not the full /v1/search API.
            api = {k: v for k, v in api.items() if k in {
                "search_target", "start_date", "end_date",
                "include_translated_tag_results", "merge_plain_keyword_results",
            }}
        native = list(api)
        if source == "search" and self.sort not in LOCAL_SORTS | PREVIEW_SORTS:
            if "sort" not in native:
                native.insert(0, "sort")  # default date_desc is server-side
        if source == "search" and self.search_target == "partial_match_for_tags":
            if "search_target" not in native:
                native.insert(0, "search_target")

        local = []
        for name in ("min_bookmarks", "max_bookmarks", "min_text_length"):
            value = getattr(self, name)
            api_key = {"min_bookmarks": "bookmark_num_min", "max_bookmarks": "bookmark_num_max",
                       "min_text_length": "text_length_min"}[name]
            if value is not None and api_key not in api:
                local.append(name)
        for name in ("min_views", "max_views", "min_pages", "max_pages", "max_text_length",
                     "bookmarked", "include_tags", "exclude_tags"):
            value = getattr(self, name)
            if value is not None and value != ():
                local.append(name)
        if self.rating != "all":
            local.append("rating")
        if self.ai_mode == "only":
            local.append("ai_mode=only")
        elif self.ai_mode == "exclude" and "search_ai_type" not in api:
            local.append("ai_mode=exclude")
        if self.originals_only is not None and "is_original_only" not in api:
            local.append("originals_only")
        if self.sort in LOCAL_SORTS:
            local.append(self.sort)
        if self.duration is not None and "duration" not in api:
            local.append("duration")
        return {"native_requested": native, "local_post_filter": local}

    def matches(self, item: dict[str, Any]) -> bool:
        def within(key: str, minimum: int | None, maximum: int | None) -> bool:
            if minimum is None and maximum is None:
                return True
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False  # Unknown is NOT zero.
            return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)

        if not (within("total_bookmarks", self.min_bookmarks, self.max_bookmarks) and
                within("total_view", self.min_views, self.max_views) and
                within("page_count", self.min_pages, self.max_pages) and
                within("text_length", self.min_text_length, self.max_text_length)):
            return False
        if self.bookmarked is not None and item.get("is_bookmarked") is not self.bookmarked:
            return False
        level = item.get("x_restrict")
        if self.rating != "all":
            if level is None:
                return False
            if self.rating == "safe" and level != 0:
                return False
            if self.rating == "r18" and level != 1:
                return False
            if self.rating == "r18g" and level != 2:
                return False
        ai_type = item.get("ai_type")
        if self.ai_mode != "any":
            if ai_type is None:
                return False
            if self.ai_mode == "exclude" and ai_type == 2:
                return False
            if self.ai_mode == "only" and ai_type != 2:
                return False
        if self.originals_only is not None and item.get("is_original") is not self.originals_only:
            return False
        norm_tags = {tag.casefold() for tag in item.get("tags", []) if isinstance(tag, str)}
        if not all(t.casefold() in norm_tags for t in self.include_tags):
            return False
        if any(t.casefold() in norm_tags for t in self.exclude_tags):
            return False
        date_str = item.get("create_date")
        if self.start_date and (not isinstance(date_str, str) or date_str[:10] < self.start_date):
            return False
        if self.end_date and (not isinstance(date_str, str) or date_str[:10] > self.end_date):
            return False
        # Also enforce locally, because limited popular-preview does not expose
        # the normal search endpoint's duration option.
        if self.duration is not None:
            if not isinstance(date_str, str):
                return False
            try:
                created = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    return False
                days = {"within_last_day": 1, "within_last_week": 7,
                        "within_last_month": 30}[self.duration]
                if created < datetime.now(timezone.utc) - timedelta(days=days):
                    return False
            except (ValueError, TypeError):
                return False
        return True

    def signature(self, query: str, kind: str) -> str:
        raw = json.dumps({"query": query, "kind": kind, "filters": asdict(self)},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def sort_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.local_sort:
            return items
        key = "total_bookmarks" if self.sort == "bookmarks_desc" else "total_view"
        return sorted(items, key=lambda x: x.get(key) if isinstance(x.get(key), int) else -1, reverse=True)


def encode_cursor(state: dict[str, Any]) -> str:
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > 3000:
        raise ValueError("Pagination cursor too large")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or len(token) > 4000:
        raise ValueError("Invalid pagination cursor")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if len(raw) > 3000:
            raise ValueError("too large")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or parsed.get("v") != 2:
            raise ValueError("incorrect version")
        return parsed
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise ValueError("Invalid pagination cursor") from exc
