"""Offline regression tests for Pixev-vs-PixivPy request mismatches.

These do not assert that the real Pixiv upstream accepts an Android identity.
"""
from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from pixiv_mcp_library.compat import (
    PIXEV_APP_HEADERS, inspect_response, make_app_api_class, pixev_app_headers,
)
from pixiv_mcp_library.engine import PixivClient, PixivEngine, parse_novel_webview_html


class StubResponse:
    def __init__(self, text, status=200, url="https://app-api.pixiv.net/v1/search/illust", content_type="application/json"):
        self.text = text
        self.status_code = status
        self.url = url
        self.headers = {"Content-Type": content_type}

    def json(self):
        return json.loads(self.text)


class BaseAPI:
    hash_secret = "test-known-hash-salt"
    hosts = "https://app-api.pixiv.net"
    user_id = 42

    def __init__(self, **kwargs):
        self.calls = []
        self.response = StubResponse('{"illusts":[],"next_url":null}')

    def auth(self, refresh_token):
        return {"response": {"refresh_token": refresh_token}}

    def no_auth_requests_call(self, method, url, headers=None, params=None, data=None, req_auth=True):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "params": params, "req_auth": req_auth})
        return self.response


def test_pixev_profile_injects_all_headers_and_valid_hash():
    comp = make_app_api_class(BaseAPI, profile="pixev")()
    comp.no_auth_requests_call("GET", "https://app-api.pixiv.net/v1/search/illust", params={"word": "五悠"}, headers={"Custom-Test": "keep"})
    call = comp.calls[0]
    for k, v in PIXEV_APP_HEADERS.items():
        assert call["headers"][k] == v
    assert call["headers"]["Custom-Test"] == "keep"
    assert call["headers"]["Accept-Language"] == "zh-CN"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", call["headers"]["X-Client-Time"])
    assert call["headers"]["X-Client-Hash"] == hashlib.md5((call["headers"]["X-Client-Time"] + comp.hash_secret).encode()).hexdigest()
    assert call["req_auth"] is True
    assert comp.last_response_diagnostic["json_keys"] == ["illusts", "next_url"]


def test_legacy_profile_does_not_change_headers():
    comp = make_app_api_class(BaseAPI, profile="legacy")()
    comp.no_auth_requests_call("GET", "https://app-api.pixiv.net/v1/search/illust", headers={"Custom-Test": "keep"})
    assert comp.calls[0]["headers"] == {"Custom-Test": "keep"}


def test_pixev_profile_does_not_mutate_non_app_api_domain():
    comp = make_app_api_class(BaseAPI, profile="pixev")()
    comp.no_auth_requests_call("GET", "https://example.com/safe", headers={"Custom-Test": "keep"})
    assert comp.calls[0]["headers"] == {"Custom-Test": "keep"}


def test_unsupported_profile_rejected():
    with pytest.raises(ValueError, match="PIXIV_APP_API_PROFILE"):
        make_app_api_class(BaseAPI, profile="weird")


def test_report_never_contains_payload_credentials_or_email():
    secret = "my-super-secret-refresh-token"
    body = json.dumps({"illusts": [], "access_token": secret, "account_email": "user@example.com"})
    res = StubResponse(body)
    record = inspect_response(res, endpoint="https://app-api.pixiv.net/v1/search/illust?token=" + secret, expected="illusts")
    assert record["expected_key_present"] is True
    serialized = json.dumps(record)
    assert secret not in serialized
    assert "user@example.com" not in serialized
    assert "token=" not in serialized


def test_report_bad_html_is_shape_not_content():
    text = '<html><script>var cf = "cloudflare"</script>Login</html>'
    res = StubResponse(text, 403, content_type="text/html")
    record = inspect_response(res, endpoint="/webview/v2/novel")
    assert record["http_status"] == 403
    assert record["json_type"] == "unrecognized_or_non_json"
    assert record["html_script_present"] and record["html_login_or_challenge_marker"]
    assert "cf =" not in json.dumps(record)


def test_diagnostic_calls_get_exact_expected_endpoints_and_redacts():
    cls = make_app_api_class(BaseAPI, profile="pixev")
    api = cls()
    client = PixivClient("fake-refresh", delay=0, api=api)
    report = client.diagnose_app_api(illust_id=93287125, novel_id=123)
    assert report["authenticated"] is True
    assert [x["path"] for x in report["checks"]] == [
        "/v1/search/illust", "/v1/user/illusts", "/v1/illust/detail", "/webview/v2/novel"
    ]
    assert report["api_profile"] in {"pixev", "legacy"}
    assert len(api.calls) == 4
    assert all(x["req_auth"] for x in api.calls)
    assert api.calls[2]["params"] == {"illust_id": 93287125}
    assert api.calls[3]["params"] == {"id": 123}
    assert "fake-refresh" not in json.dumps(report)


def test_native_malformed_response_adds_only_safe_diagnostics():
    from pixiv_mcp_library.advanced import SearchOptions
    class BadSearch(BaseAPI):
        def parse_result(self, raw):
            return json.loads(raw.text)
    bad = BadSearch()
    bad.response = StubResponse('{"error":{"message":"access_token=SECRET"},"another":{}}', status=200)
    engine = PixivEngine(PixivClient("fake", delay=0, api=bad))
    with pytest.raises(RuntimeError) as exc:
        engine.search_advanced(query="test", kind="illust", options=SearchOptions.create(lang=None, min_bookmarks=10))
    assert "redacted" in str(exc.value)
    assert "SECRET" not in str(exc.value)


def test_pixev_style_parser_can_skip_whitespace_before_json_object():
    page = '<script>window.__data = { novel: ( {"id":"7","title":"A","text":"works"} ) }; </script>'
    assert parse_novel_webview_html(page)["text"] == "works"
