"""Exhaustive Pixev-compatibility checks using a simulated Pixiv App API.

These validate our outgoing parameters and local fallbacks. They do not make
claims about live Pixiv server accepting any particular language code.
"""

import pytest

from pixiv_mcp_library.advanced import SearchOptions, decode_cursor
from pixiv_mcp_library.engine import PixivClient, PixivEngine
from test_advanced import AdvancedFakeAPI


def engine_with(api):
    return PixivEngine(PixivClient("test-refresh", delay=0, api=api))


def test_pixev_novel_search_all_native_features():
    api = AdvancedFakeAPI()
    engine = engine_with(api)
    opts = SearchOptions.create(
        language="zh_cn", search_target="keyword", sort="popular_desc",
        start_date="2025-03-01", end_date="2026-10-30",
        min_bookmarks=100, max_bookmarks=500,
        min_text_length=5000, max_text_length=30000,
        originals_only=True, is_replaceable_only=True,
        include_potential_violation_works=False,
        include_translated_tag_results=False,
        merge_plain_keyword_results=False,
        ai_mode="exclude", bookmarked=True,
    )
    result = engine.search_advanced(query="五悠", kind="novel", options=opts)
    assert len(api.raw_calls) == 1
    method, url, params, req_auth = api.raw_calls[0]
    assert method == "GET" and url.endswith("/v1/search/novel") and req_auth is True
    assert params == {
        "word": "五悠", "search_target": "keyword", "sort": "popular_desc",
        "filter": "for_android", "start_date": "2025-03-01", "end_date": "2026-10-30",
        "bookmark_num_min": 100, "bookmark_num": 100, "bookmark_num_max": 500,
        "text_length_min": 5000, "lang": "zh-CN",
        "search_ai_type": 0, "is_original_only": "true", "is_replaceable_only": "true",
        "include_potential_violation_works": "false", "include_translated_tag_results": "false",
        "merge_plain_keyword_results": "false",
    }
    plan = result["filter_execution"]
    assert {"lang", "bookmark_num_min", "bookmark_num_max", "text_length_min",
            "is_original_only", "is_replaceable_only", "search_ai_type"} <= set(plan["native_requested"])
    assert {"max_text_length", "bookmarked"} <= set(plan["local_post_filter"])
    assert "min_bookmarks" not in plan["local_post_filter"]
    assert result["items"] == []  # fake novels are non-original, local safety check
    assert result["sort_scope"] == "pixiv_order"


def test_pixev_artwork_bookmark_range_ai_and_duration_natively():
    api = AdvancedFakeAPI()
    engine = engine_with(api)
    opts = SearchOptions.create(sort="popular_male_desc", search_target="exact_match_for_tags",
        min_bookmarks=50, max_bookmarks=250, duration="within_last_month", ai_mode="exclude",
        merge_plain_keyword_results=True, include_translated_tag_results=True)
    result = engine.search_advanced(query="五悠", kind="manga", options=opts)
    assert [w["id"] for w in result["items"]] == [1]
    assert len(api.raw_calls) == 2  # two Pixiv pages, both native
    for method, url, params, auth in api.raw_calls:
        assert url.endswith("/v1/search/illust") and auth is True
        assert params["bookmark_num_min"] == 50
        assert params["bookmark_num_max"] == 250
        assert params["search_ai_type"] == 0  # Python SDK silently skips this value
        assert params["sort"] == "popular_male_desc"
        assert params["duration"] == "within_last_month"
        assert params["merge_plain_keyword_results"] == "true"
        assert params["include_translated_tag_results"] == "true"
    assert "bookmark_num_min" in result["filter_execution"]["native_requested"]


def test_default_ai_any_explicitly_includes_ai():
    api = AdvancedFakeAPI()
    engine = engine_with(api)
    engine.search_advanced(query="五悠", kind="illust")
    assert api.raw_calls == []  # wrapper can handle nonzero AI setting
    assert api.calls[0][2]["search_ai_type"] == 1


def test_native_language_cursor_and_download_discovery_use_same_api():
    api = AdvancedFakeAPI()
    engine = engine_with(api)
    opts = SearchOptions.create(lang="ja", min_bookmarks=100)
    first = engine.search_advanced(query="五悠", kind="novel", options=opts, page_size=1)
    assert first["items"][0]["id"] == 102  # 101 fails postfilter
    assert first["has_more"]
    second = engine.search_advanced(query="五悠", kind="novel", options=opts,
                                    cursor=first["next_cursor"], page_size=1)
    assert second["items"] == []
    assert len(api.raw_calls) == 2
    assert all(call[2]["lang"] == "ja" and call[2]["bookmark_num_min"] == 100
               for call in api.raw_calls)
    works = engine.list_works(source="search", kind="novel", query="五悠", limit=1,
                              options=opts)
    assert [x["id"] for x in works] == [102]
    assert api.raw_calls[-1][2]["lang"] == "ja"


class UnsupportedNativeAPI(AdvancedFakeAPI):
    def no_auth_requests_call(self, method, url, **kwargs):
        self.raw_calls.append((method, url, kwargs["params"], kwargs.get("req_auth")))
        raise ValueError("unsupported parameter: bookmark_num_min")


def test_falls_back_only_to_locally_verifiable_fields():
    api = UnsupportedNativeAPI()
    engine = engine_with(api)
    opts = SearchOptions.create(min_bookmarks=100, max_bookmarks=250,
                                min_text_length=5000, originals_only=False,
                                ai_mode="exclude")
    out = engine.search_advanced(query="五悠", kind="novel", options=opts)
    assert [x["id"] for x in out["items"]] == [102]
    assert len(api.raw_calls) == 1
    assert api.calls[0][2].get("search_ai_type") is None
    assert {"min_bookmarks", "max_bookmarks", "min_text_length",
            "ai_mode"} <= set(out["filter_execution"]["fallback_to_local"])
    assert "originals_only" in out["filter_execution"]["local_post_filter"]
    assert "min_bookmarks" in out["filter_execution"]["local_post_filter"]
    assert "bookmark_num_min" not in out["filter_execution"]["native_requested"]


def test_missing_raw_api_falls_back_when_metadata_is_sufficient():
    api = AdvancedFakeAPI()
    api.no_auth_requests_call = None
    engine = engine_with(api)
    page = engine.search_advanced(query="五悠", kind="manga",
                                  options=SearchOptions.create(min_bookmarks=100))
    assert [x["id"] for x in page["items"]] == [1]
    assert page["filter_execution"]["fallback_to_local"] == ["min_bookmarks"]


def test_no_silent_language_downgrade():
    api = UnsupportedNativeAPI()
    engine = engine_with(api)
    with pytest.raises(RuntimeError, match="lang"):
        engine.search_advanced(query="五悠", kind="novel",
                               options=SearchOptions.create(lang="ja", min_bookmarks=100))
    assert api.calls == []  # never silently send an unfiltered search


def test_native_non_param_errors_never_retry_as_unfiltered():
    class RateLimited(UnsupportedNativeAPI):
        def no_auth_requests_call(self, method, url, **kwargs):
            raise RuntimeError("429 Too Many Requests")
    api = RateLimited()
    with pytest.raises(RuntimeError, match="429"):
        engine_with(api).search_advanced(query="X", kind="manga",
                   options=SearchOptions.create(min_bookmarks=50))
    assert api.calls == []


def test_language_mode_validation_and_alias():
    assert SearchOptions.create(language="JA").lang == "ja"
    assert SearchOptions.create(lang="zh-cn").lang == "zh-CN"
    for value in ("../evil", "ja?foo=bar", "", "unduly-long-code", 15):
        if value == "":
            assert SearchOptions.create(lang=value).lang is None
        else:
            with pytest.raises(ValueError, match="lang"):
                SearchOptions.create(lang=value)
    with pytest.raises(ValueError, match="disagree"):
        SearchOptions.create(language="ja", lang="zh-CN")
    with pytest.raises(ValueError, match="Novel"):
        SearchOptions.create(lang="ja").validate_kind("manga")
    with pytest.raises(ValueError, match="kind=novel"):
        SearchOptions.create(lang="ja").validate_kind("all")
    with pytest.raises(ValueError, match="popular_preview"):
        SearchOptions.create(lang="ja", sort="popular_preview").validate_kind("novel")
    with pytest.raises(ValueError, match="search-only"):
        engine_with(AdvancedFakeAPI()).list_works(source="user", kind="novel",
                       options=SearchOptions.create(lang="ja"))


def test_local_only_filters_kept_local():
    api = AdvancedFakeAPI()
    page = engine_with(api).search_advanced(query="novel", kind="novel",
             options=SearchOptions.create(max_text_length=300, min_views=1, originals_only=False))
    assert len(page["items"]) == 1 and page["items"][0]["id"] == 103
    assert len(api.raw_calls) == 0
    assert {"max_text_length", "min_views", "originals_only"} <= set(page["filter_execution"]["local_post_filter"])


def test_user_list_local_fallback_keeps_paginating_to_fill_requested_limit():
    from test_advanced import mock_novel

    class FakeArtist(AdvancedFakeAPI):
        def user_novels(self, user_id, offset=None):
            self.calls.append(("user_novels", user_id, offset))
            if not offset:
                return {"novels": [mock_novel(101, bookmarks=1)],
                        "next_url": "https://app-api.pixiv.net/v1/user/novels?offset=10"}
            return {"novels": [mock_novel(102, bookmarks=300)], "next_url": None}

    api = FakeArtist()
    works = engine_with(api).list_works(source="user", kind="novel", user_id=44,
                  limit=1, options=SearchOptions.create(min_bookmarks=100))
    assert [x["id"] for x in works] == [102]
    assert api.calls == [("user_novels", 44, None), ("user_novels", 44, "10")]
    assert not api.raw_calls  # user listings have no native bookmark threshold


def test_popular_preview_duration_filter_falls_back_locally():
    api = AdvancedFakeAPI()
    page = engine_with(api).search_advanced(query="test", kind="illust",
        options=SearchOptions.create(sort="popular_preview", duration="within_last_week"))
    assert page["items"] == []  # fake works are from 2026-10-15
    assert "duration" in page["filter_execution"]["local_post_filter"]
    assert "duration" not in page["filter_execution"]["native_requested"]


def test_start_download_uses_native_novel_search_options(tmp_path):
    from pixiv_mcp_library.downloads import JobManager
    import time

    class FakeDownloader:
        def save_novel(self, work_id, fmt):
            return {"folder": str(tmp_path), "files": [{"status": "downloaded"}]}

    api = AdvancedFakeAPI()
    engine = engine_with(api)
    manager = JobManager(engine, FakeDownloader(), max_batch=2)
    filters = SearchOptions.create(lang="ja", min_bookmarks=100,
                                   search_target="keyword", min_text_length=5000)
    started = manager.start(source="search", kind="novel", query="五悠", limit=1,
                            options=filters)
    for _ in range(100):
        status = manager.status(started["job_id"])
        if status["state"] in {"done", "failed", "partial", "canceled"}:
            break
        time.sleep(.01)
    assert status["state"] == "done"
    assert status["total"] == 1 and status["finished"] == 1
    assert api.raw_calls[-1][2]["lang"] == "ja"
    assert api.raw_calls[-1][2]["bookmark_num_min"] == 100
    assert api.raw_calls[-1][2]["text_length_min"] == 5000
    with pytest.raises(ValueError, match="source=search"):
        manager.start(source="user", kind="novel", user_id=44, options=filters)
    with pytest.raises(ValueError, match="filters"):
        manager.start(source="ids", kind="novel", ids=[101], options=filters)


def test_premium_rejects_native_bookmark_range_then_uses_local():
    class PremiumRangeBlocked(UnsupportedNativeAPI):
        def no_auth_requests_call(self, method, url, **kwargs):
            self.raw_calls.append((method, url, kwargs['params'], kwargs.get('req_auth')))
            raise RuntimeError('403 Forbidden: Premium feature')

    api = PremiumRangeBlocked()
    result = engine_with(api).search_advanced(query='五悠', kind='manga',
          options=SearchOptions.create(min_bookmarks=100, max_bookmarks=250))
    assert [work['id'] for work in result['items']] == [1]
    assert set(result['filter_execution']['fallback_to_local']) == {'min_bookmarks', 'max_bookmarks'}
    assert len(api.raw_calls) == 2  # fallback on each upstream page


def test_strict_api_only_filter_never_downgrades_even_with_403():
    class RejectLang(UnsupportedNativeAPI):
        def no_auth_requests_call(self, method, url, **kwargs):
            raise RuntimeError('403 Forbidden')

    with pytest.raises(RuntimeError, match='lang'):
        engine_with(RejectLang()).search_advanced(query='五悠', kind='novel',
            options=SearchOptions.create(lang='ja'))


def test_malformed_native_response_is_not_mistaken_for_empty_results():
    class BadResponse(AdvancedFakeAPI):
        def no_auth_requests_call(self, method, url, **kwargs):
            return {'unexpected_field': []}

    with pytest.raises(RuntimeError, match='did not include'):
        engine_with(BadResponse()).search_advanced(query='五悠', kind='novel',
            options=SearchOptions.create(lang='ja'))
