"""Popularity search semantics checked against simulated App API calls."""

import pytest

from pixiv_mcp_library.advanced import SearchOptions, decode_cursor
from pixiv_mcp_library.engine import PixivClient, PixivEngine


def art(wid, kind="illust"):
    return {"id": wid, "type": kind, "title": f"Art {wid}", "user": {"id": 13, "name": "A"},
            "tags": [], "image_urls": {"medium": "https://i.pximg.net/foo.jpg"}}


def novel(wid):
    return {"id": wid, "title": f"Novel {wid}", "user": {"id": 13, "name": "B"},
            "tags": [], "image_urls": {"medium": "https://i.pximg.net/cover.jpg"}}


class FakeApp:
    hosts = "https://app-api.pixiv.net"
    user_id = 13

    def __init__(self):
        self.calls = []

    def auth(self, refresh_token):
        return {"response": {"refresh_token": refresh_token}}

    def search_illust(self, word, **kwargs):
        self.calls.append(("search_illust", word, kwargs))
        return {"illusts": [art(1), art(2, "manga")], "next_url": None}

    def search_novel(self, word, **kwargs):
        self.calls.append(("search_novel", word, kwargs))
        return {"novels": [novel(101), novel(102)], "next_url": None}

    def no_auth_requests_call(self, method, url, **kwargs):
        self.calls.append(("raw", method, url, kwargs))
        if url.endswith("/novel"):
            return {"novels": [novel(101), novel(102)],
                    "next_url": "https://app-api.pixiv.net/v1/search/novel?offset=500"}
        return {"illusts": [art(1), art(2, "manga")],
                "next_url": "https://app-api.pixiv.net/v1/search/illust?offset=500"}

    def parse_result(self, result):
        return result


@pytest.fixture
def engine():
    return PixivEngine(PixivClient("unit-test-token", delay=0, api=FakeApp()))


def test_novel_popular_is_pixiv_native(engine):
    opts = SearchOptions.create(sort="popular_desc", search_target="keyword")
    result = engine.search_advanced(query="五悠", kind="novel", options=opts)
    assert result["sort_scope"] == "pixiv_order"
    assert [x["id"] for x in result["items"]] == [101, 102]
    assert engine.client.api.calls == [("search_novel", "五悠", {"sort": "popular_desc", "search_target": "keyword", "search_ai_type": 1})]


def test_manga_popular_is_pixiv_native(engine):
    result = engine.search_advanced(query="漫画", kind="manga", options=SearchOptions.create(sort="popular_desc"))
    assert [x["id"] for x in result["items"]] == [2]
    assert engine.client.api.calls[0][0] == "search_illust"
    assert engine.client.api.calls[0][2]["sort"] == "popular_desc"
    assert result["sort_scope"] == "pixiv_order"


def test_pixev_gender_popular_sorts_artwork_only(engine):
    result = engine.search_advanced(query="动漫", kind="illust", options=SearchOptions.create(sort="popular_female_desc"))
    assert result["sort_scope"] == "pixiv_order"
    assert engine.client.api.calls[0][2]["sort"] == "popular_female_desc"
    with pytest.raises(ValueError, match="gender-specific"):
        engine.search_advanced(query="novel", kind="novel", options=SearchOptions.create(sort="popular_male_desc"))


def test_all_popular_is_per_type_not_globally_interleaved(engine):
    result = engine.search_advanced(query="love", kind="all", options=SearchOptions.create(sort="popular_desc"))
    assert result["sort_scope"] == "per_type_pixiv_order"
    assert [x["id"] for x in result["items"]] == [1, 2, 101, 102]
    calls = engine.client.api.calls
    assert [x[0] for x in calls] == ["search_illust", "search_illust", "search_novel"]
    assert all(x[2]["sort"] == "popular_desc" for x in calls)


def test_nonpremium_artwork_preview_does_not_claim_premium_or_follow_next_url(engine):
    opts = SearchOptions.create(sort="popular_preview")
    first = engine.search_advanced(query="五悠", kind="illust", page_size=1, options=opts)
    assert first["sort_scope"] == "limited_popular_preview"
    assert [x["id"] for x in first["items"]] == [1]
    # second preview object is a manga: filter it out and do not follow next_url
    second = engine.search_advanced(query="五悠", kind="illust", page_size=1,
                                    cursor=first["next_cursor"], options=opts)
    assert second["items"] == [] and second["next_cursor"] is None
    assert len(engine.client.api.calls) == 2
    for entry in engine.client.api.calls:
        assert entry[0] == "raw"
        assert entry[2] == "https://app-api.pixiv.net/v1/search/popular-preview/illust"
        assert "sort" not in entry[3]["params"]
        assert entry[3]["req_auth"] is True


def test_nonpremium_novel_preview_is_limited_and_supports_keyword(engine):
    opts = SearchOptions.create(sort="popular_preview", search_target="keyword")
    result = engine.search_advanced(query="五悠", kind="novel", page_size=50, options=opts)
    assert [x["id"] for x in result["items"]] == [101, 102]
    assert result["sort_scope"] == "limited_popular_preview"
    assert not result["has_more"]
    url = engine.client.api.calls[0][2]
    assert url.endswith("/search/popular-preview/novel")
    assert engine.client.api.calls[0][3]["params"]["search_target"] == "keyword"


def test_bulk_discovery_uses_same_native_popular_order(engine):
    opts = SearchOptions.create(sort="popular_desc")
    works = engine.list_works(source="search", query="五悠", kind="novel", limit=2, options=opts)
    assert [x["id"] for x in works] == [101, 102]
    assert engine.client.api.calls[0][2]["sort"] == "popular_desc"


def test_preview_rejects_mixed_search():
    with pytest.raises(ValueError, match="kind=all"):
        SearchOptions.create(sort="popular_preview").validate_kind("all")
