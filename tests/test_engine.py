import pytest

from pixiv_mcp_library.engine import PixivClient, PixivEngine


class FakeAPI:
    user_id = 42

    def __init__(self):
        self.calls = []
        self.auth_count = 0

    def auth(self, refresh_token):
        self.auth_count += 1
        return {"response": {"refresh_token": "updated-refresh-token"}}

    def search_novel(self, word, offset=None, **kwargs):
        self.calls.append(("search_novel", word, offset))
        if not offset:
            return {"novels": [self._novel(10)], "next_url": "https://app-api.pixiv.net/search?offset=1"}
        return {"novels": [self._novel(20)], "next_url": None}

    def search_illust(self, word, offset=None, **kwargs):
        self.calls.append(("search_illust", word, offset))
        return {"illusts": [self._art(11, "manga"), self._art(12, "illust")], "next_url": None}

    def user_illusts(self, user_id, type, offset=None):
        self.calls.append(("user_illusts", user_id, type, offset))
        return {"illusts": [self._art(11 if type == "manga" else 12, type)], "next_url": None}

    def user_novels(self, user_id, offset=None):
        self.calls.append(("user_novels", user_id, offset))
        return {"novels": [self._novel(10)], "next_url": None}

    def novel_series(self, series_id, last_order=None):
        self.calls.append(("novel_series", series_id, last_order))
        if not last_order:
            return {"novels": [self._novel(10)], "next_url": "https://app-api.pixiv.net/v2/novel/series?last_order=10"}
        return {"novels": [self._novel(20)], "next_url": None}

    def novel_detail(self, novel_id):
        return self._novel(novel_id)

    def webview_novel(self, novel_id):
        return {"title": f"Novel {novel_id}", "text": "Part 1\nPart 2\nPart 3"}

    def illust_detail(self, illust_id):
        return {"illust": self._art(illust_id, "manga")}

    @staticmethod
    def _novel(id):
        return {"id": id, "title": f"Novel {id}", "user": {"id": 42, "name": "Author"}, "tags": [{"name": "tag"}]}

    @staticmethod
    def _art(id, type):
        return {"id": id, "title": f"Art {id}", "type": type, "user": {"id": 42, "name": "Author"}, "tags": []}


@pytest.fixture
def engine():
    api = FakeAPI()
    client = PixivClient(refresh_token="secret", api=api, delay=0)
    return PixivEngine(client)


def test_login_refresh_token_in_memory(engine):
    assert engine.client.user_id == 42
    assert engine.client.refresh_token == "updated-refresh-token"
    assert engine.client.api.auth_count == 1


def test_novel_search_pagination_and_read(engine):
    items = engine.list_works(source="search", kind="novel", query="hello", limit=10)
    assert [x["id"] for x in items] == [10, 20]
    assert items[0]["url"].startswith("https://www.pixiv.net/novel/")
    chunk = engine.read_novel(10, offset=7, max_chars=6)
    assert chunk["text"] == "Part 2"
    assert chunk["has_more"] is True


def test_manga_filter(engine):
    works = engine.list_works(source="search", kind="manga", query="test", limit=10)
    assert len(works) == 1 and works[0]["id"] == 11


def test_series_cursor(engine):
    works = engine.list_works(source="series", kind="novel", series_id=55)
    assert [x["id"] for x in works] == [10, 20]
    assert ("novel_series", 55, "10") in engine.client.api.calls


def test_user_has_all_three_kinds(engine):
    works = engine.list_works(source="user", kind="all", user_id=42, limit=9)
    assert {x["kind"] for x in works} == {"illust", "manga", "novel"}


def test_rejects_missing_query_or_invalid_id(engine):
    with pytest.raises(ValueError):
        engine.list_works(source="search", kind="novel")
    with pytest.raises(ValueError):
        engine.get_work(0, "novel")


def test_pixez_style_webview_fallback():
    from pixiv_mcp_library.engine import parse_novel_webview_html
    html = '<script>const p = { novel: {"id":"123", "title":"Test", "text":"a {brace} \\"quoted\\" text"}, isOwnWork: false };</script>'
    assert parse_novel_webview_html(html)["text"] == 'a {brace} "quoted" text'


def test_webview_fallback_invoked(engine):
    def broken_webview(novel_id, raw=False):
        if raw:
            return '<script> window.foo={novel: {"id":"10", "text":"ok"}, isOwnWork:false}; </script>'
        raise Exception('Extract novel content error')
    engine.client.api.webview_novel = broken_webview
    assert engine.read_novel(10)["text"] == "ok"
