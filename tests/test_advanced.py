"""API-adapter contract tests; no Pixiv network or real token is used."""
import base64
import json

import pytest

from pixiv_mcp_library.advanced import SearchOptions, decode_cursor
from pixiv_mcp_library.engine import PixivClient, PixivEngine, work_summary
from pixiv_mcp_library.downloads import JobManager


def mock_work(id, kind="illust", *, bookmarks=10, views=100, pages=1, tags=None):
    return {
        "id": id,
        "type": kind,
        "title": f"Artwork {id}",
        "caption": "A <b>great</b> example &amp; more",
        "user": {"id": 123, "name": "Alice", "account": "alice", "profile_image_urls": {"medium": "avatar"}},
        "image_urls": {"square_medium": "https://i.pximg.net/px-square.jpg",
                       "medium": "https://i.pximg.net/medium.jpg", "large": "https://i.pximg.net/large.jpg"},
        "total_bookmarks": bookmarks,
        "total_view": views,
        "total_comments": 3,
        "create_date": "2026-10-15T12:00:00+09:00",
        "is_bookmarked": id % 2 == 0,
        "illust_ai_type": 2 if id == 4 else 0,
        "page_count": pages,
        "x_restrict": 0,
        "tags": [{"name": tag, "translated_name": f"{tag}-en"} for tag in (tags or ["五悠", "漫畫"])],
    }


def mock_novel(id, *, bookmarks=45, views=200, length=8000):
    return {
        "id": id,
        "title": f"Novel {id}",
        "caption": "Short novel",
        "user": {"id": 44, "name": "Bob"},
        "image_urls": {"square_medium": "https://i.pximg.net/cover.jpg", "medium": "https://i.pximg.net/cover.jpg"},
        "tags": [{"name": "小説"}],
        "total_bookmarks": bookmarks,
        "total_view": views,
        "total_comments": 1,
        "page_count": 4,
        "text_length": length,
        "is_bookmarked": True,
        "is_original": False,
        "novel_ai_type": 0,
        "x_restrict": 0,
        "create_date": "2026-10-15T12:00:00+09:00",
        "series": {"id": 88, "title": "Series"},
    }


class AdvancedFakeAPI:
    user_id = 44
    hosts = "https://app-api.pixiv.net"

    def __init__(self):
        self.calls = []
        self.raw_calls = []
        self.art = [mock_work(1, "manga", bookmarks=200), mock_work(2, "illust", bookmarks=60),
                    mock_work(3, "manga", bookmarks=10, pages=24), mock_work(4, "illust", bookmarks=300)]
        self.novels = [mock_novel(101), mock_novel(102, bookmarks=200), mock_novel(103, length=100)]

    def auth(self, refresh_token):
        return {"response": {"refresh_token": refresh_token}}

    def search_illust(self, word, **kwargs):
        self.calls.append(("search_illust", word, kwargs))
        offset = kwargs.get("offset")
        if offset is None:
            return {"illusts": self.art[:2], "next_url": "https://app-api.pixiv.net/v1/search/illust?offset=2"}
        return {"illusts": self.art[2:], "next_url": None}

    def search_novel(self, word, **kwargs):
        self.calls.append(("search_novel", word, kwargs))
        return {"novels": self.novels, "next_url": None}

    def no_auth_requests_call(self, method, url, **kwargs):
        params = kwargs["params"]
        self.raw_calls.append((method, url, dict(params), kwargs.get("req_auth")))
        options = {k: v for k, v in params.items() if k not in {"word", "filter"}}
        if url.endswith("/novel"):
            return self.search_novel(params["word"], **options)
        return self.search_illust(params["word"], **options)

    def parse_result(self, result):
        return result

    def novel_detail(self, id):
        return mock_novel(id)

    def illust_detail(self, id):
        return {"illust": mock_work(id, "manga", pages=3)}

    def webview_novel(self, id):
        return {"id": id, "title": f"Novel {id}", "text": "Hello world",
                "rating": {"like": 1337}}


@pytest.fixture
def engine():
    return PixivEngine(PixivClient("secret", delay=0, api=AdvancedFakeAPI()))


def test_rich_metadata_for_all_categories(engine):
    artwork = work_summary(mock_work(2), "illust")
    assert artwork["thumbnail_url"].endswith("px-square.jpg")
    assert artwork["cover_url"].endswith("medium.jpg")
    assert artwork["user_account"] == "alice"
    assert artwork["tag_details"] == [
        {"name": "五悠", "translated_name": "五悠-en"},
        {"name": "漫畫", "translated_name": "漫畫-en"},
    ]
    assert artwork["total_bookmarks"] == 10
    assert artwork["total_view"] == 100
    assert artwork["total_likes"] is None
    assert artwork["likes_source"] == "unavailable"
    assert artwork["description"] == "A great example & more"
    assert artwork["is_bookmarked"] is True
    assert artwork["ai_type"] == 0
    assert artwork["create_date"].startswith("2026-10-15")
    novel = work_summary(mock_novel(101), "novel")
    assert novel["text_length"] == 8000
    assert novel["series"] == {"id": 88, "title": "Series"}
    assert novel["cover_url"]
    assert novel["total_comments"] == 1
    assert novel["url"].endswith("id=101")


def test_metadata_unknown_not_zero():
    info = work_summary({"id": 1, "user": {}, "tags": []}, "illust")
    assert info["total_view"] is None and info["total_bookmarks"] is None
    assert not SearchOptions.create(min_bookmarks=0).matches(info)


def test_advanced_native_search_query_parameters(engine):
    opts = SearchOptions.create(search_target="exact_match_for_tags", sort="date_asc",
                                start_date="2026-10-01", end_date="2026-10-31")
    page = engine.search_advanced(query="五悠", kind="illust", options=opts)
    assert [x["id"] for x in page["items"]] == [2, 4]
    call = engine.client.api.calls[0]
    assert call[2]["search_target"] == "exact_match_for_tags"
    assert call[2]["sort"] == "date_asc"
    assert call[2]["start_date"] == "2026-10-01"
    assert call[2]["end_date"] == "2026-10-31"


def test_novel_search_text_and_length(engine):
    opts = SearchOptions.create(search_target="text", min_text_length=1000, min_bookmarks=40)
    page = engine.search_advanced(query="小说正文", kind="novel", options=opts)
    assert [x["id"] for x in page["items"]] == [101, 102]
    assert engine.client.api.calls[0][2]["search_target"] == "text"
    assert engine.client.api.raw_calls[0][2]["text_length_min"] == 1000
    assert engine.client.api.raw_calls[0][2]["bookmark_num_min"] == 40
    assert page["next_cursor"] is None


def test_exact_cursor_resume_while_upstream_page_has_more_items(engine):
    first = engine.search_advanced(query="foo", kind="manga", page_size=1, max_scan=100)
    assert [x["id"] for x in first["items"]] == [1]
    assert first["has_more"]
    decoded = decode_cursor(first["next_cursor"])
    assert decoded["offset"] is None and decoded["index"] == 1
    second = engine.search_advanced(query="foo", kind="manga", page_size=1, cursor=first["next_cursor"])
    assert [x["id"] for x in second["items"]] == [3]
    last = engine.search_advanced(query="foo", kind="manga", page_size=1, cursor=second["next_cursor"])
    assert last["items"] == [] and last["next_cursor"] is None


def test_max_scan_returns_cursor_even_with_no_matches(engine):
    opts = SearchOptions.create(min_bookmarks=99999)
    page = engine.search_advanced(query="foo", kind="manga", page_size=2, max_scan=1, options=opts)
    assert page["items"] == []
    assert page["scanned_count"] == 1
    assert page["next_cursor"]


def test_filter_and_local_sort(engine):
    opts = SearchOptions.create(sort="bookmarks_desc", min_bookmarks=40,
                                bookmarked=True, ai_mode="exclude")
    page = engine.search_advanced(query="foo", kind="illust", page_size=10, options=opts)
    assert [x["id"] for x in page["items"]] == [2]
    assert page["sort_scope"] == "scanned_window"
    assert all(call[2].get("sort", "date_desc") == "date_desc" for call in engine.client.api.calls)


def test_novel_likes_enrichment(engine):
    opts = SearchOptions.create(include_likes=True)
    results = engine.search_advanced(query="foo", kind="novel", page_size=10, options=opts)
    assert all(work["total_likes"] == 1337 for work in results["items"])
    assert all(work["likes_source"] == "novel_webview" for work in results["items"])
    with pytest.raises(ValueError, match="<=20"):
        engine.search_advanced(query="foo", kind="novel", page_size=21, options=opts)


def test_cursor_invalid_change_query_and_options(engine):
    first = engine.search_advanced(query="foo", kind="novel", page_size=1)
    with pytest.raises(ValueError, match="does not match"):
        engine.search_advanced(query="bar", kind="novel", page_size=1, cursor=first["next_cursor"])
    with pytest.raises(ValueError, match="does not match"):
        engine.search_advanced(query="foo", kind="novel", page_size=1, cursor=first["next_cursor"],
                               options=SearchOptions.create(min_views=12))
    with pytest.raises(ValueError, match="Invalid pagination cursor"):
        engine.search_advanced(query="foo", kind="novel", cursor="!!!")


def test_invalid_advanced_filters_rejected():
    with pytest.raises(ValueError, match="after"):
        SearchOptions.create(start_date="2026-12-31", end_date="2026-01-01")
    with pytest.raises(ValueError, match="max_bookmarks"):
        SearchOptions.create(min_bookmarks=200, max_bookmarks=100)
    with pytest.raises(ValueError, match="non-negative"):
        SearchOptions.create(min_views=-1)
    SearchOptions.create(sort="popular_desc").validate_kind("novel")
    with pytest.raises(ValueError, match="gender-specific"):
        SearchOptions.create(sort="popular_male_desc").validate_kind("novel")
    with pytest.raises(ValueError, match="unsupported"):
        SearchOptions.create(search_target="text").validate_kind("manga")
    with pytest.raises(ValueError, match="duration"):
        SearchOptions.create(duration="within_last_month").validate_kind("novel")


def test_all_kind_paged_and_no_repeats(engine):
    page1 = engine.search_advanced(query="foo", kind="all", page_size=6)
    ids1 = {(x["id"], x["kind"]) for x in page1["items"]}
    assert ids1 == {(2, "illust"), (4, "illust"), (1, "manga"), (3, "manga"), (101, "novel"), (102, "novel")}
    page2 = engine.search_advanced(query="foo", kind="all", page_size=6, cursor=page1["next_cursor"])
    assert len(page2["items"]) == 1 and page2["items"][0]["id"] == 103
    assert page2["next_cursor"] is None


def test_list_works_and_bulk_preview_share_filters(engine):
    opts = SearchOptions.create(min_bookmarks=100, include_tags=["五悠"], sort="date_desc")
    listed = engine.list_works(source="search", kind="manga", query="foo", limit=10, options=opts)
    advanced = engine.search_advanced(query="foo", kind="manga", options=opts)
    assert [x["id"] for x in listed] == [x["id"] for x in advanced["items"]] == [1]


def test_compact_mode_excludes_verbose_fields_and_keeps_metrics(engine):
    page = engine.search_advanced(query="foo", kind="illust", view="compact")
    assert page["view"] == "compact"
    assert page["items"][0]["thumbnail_url"]
    assert "total_bookmarks" in page["items"][0]
    assert "caption" not in page["items"][0]
    assert "image_urls" not in page["items"][0]
    with pytest.raises(ValueError, match="view"):
        engine.search_advanced(query="foo", kind="illust", view="raw_binary")


def test_bookmarks_sort_window_preserves_all_results_and_resumes(engine):
    opts = SearchOptions.create(sort="bookmarks_desc")
    first = engine.search_advanced(query="foo", kind="illust", page_size=1, max_scan=4, options=opts)
    assert [x['id'] for x in first['items']] == [4]
    assert first['scanned_count'] == 4
    assert first['sampled_matches'] == 2
    second = engine.search_advanced(query="foo", kind="illust", page_size=1,
                                    max_scan=4, cursor=first['next_cursor'], options=opts)
    assert [x['id'] for x in second['items']] == [2]
    assert second['scanned_count'] == 0  # served from bounded, sorted cache
    assert second['next_cursor'] is None


def test_bulk_list_from_sorted_window_not_capped_at_first_page(engine):
    engine.client.api.art = [mock_work(i, bookmarks=i) for i in range(70)]
    opts = SearchOptions.create(sort='bookmarks_desc')
    listed = engine.list_works(source='search', kind='illust', query='foo', limit=60, options=opts)
    assert len(listed) == 60
    assert [x['total_bookmarks'] for x in listed] == list(range(69, 9, -1))
