"""Pixev-compatible App API request headers and *redacted* response diagnostics.

Pixev (dandibbert/pixez-flutter) uses Android identity and X-Client-* on its
App API Dio client. PixivPy3's default App API calls still identify as an old
iOS build; OAuth success does not validate that the App API returned data.
This module does not patch PixivPy3 globally and never exposes OAuth tokens.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse


PIXEV_APP_HEADERS = {
    "User-Agent": "PixivAndroidApp/5.0.155 (Android 10.0; Pixel C)",
    "App-OS": "Android",
    "App-OS-Version": "Android 10.0",
    "App-Version": "5.0.166",
}


def pixev_app_headers(hash_secret: str, accept_language: str = "zh-CN") -> dict[str, str]:
    """Create Android App API headers analogous to Pixev's, with fresh time."""
    client_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return {
        **PIXEV_APP_HEADERS,
        "Accept-Language": accept_language,
        "X-Client-Time": client_time,
        "X-Client-Hash": hashlib.md5((client_time + hash_secret).encode("utf-8")).hexdigest(),
    }


def inspect_response(response: Any, *, endpoint: str, expected: str | None = None) -> dict[str, Any]:
    """Summarize HTTP *structure*, never payload, credentials, cookies or URLs.

    Important: even valid Pixiv JSON may contain private account information;
    keep only sanitized top-level field names and boolean indicators.
    """
    status = getattr(response, "status_code", None)
    header_map = getattr(response, "headers", None) or {}
    content_type = str(header_map.get("Content-Type", header_map.get("content-type", ""))).split(";")[0][:80]
    body = getattr(response, "text", "")
    if not isinstance(body, str):
        body = ""
    hostname = urlparse(str(getattr(response, "url", "") or "")).hostname
    path = urlparse(endpoint).path or endpoint
    summary: dict[str, Any] = {
        "path": path[:128],
        "http_status": status,
        "content_type": content_type,
        "bytes_approx": len(body.encode("utf-8", errors="replace")),
        "response_host": hostname if hostname in {"app-api.pixiv.net", "oauth.secure.pixiv.net"} else "other_or_unknown",
    }
    try:
        parsed = response.json() if callable(getattr(response, "json", None)) else json.loads(body)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        keys = [key for key in parsed if isinstance(key, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", key)]
        summary["json_keys"] = sorted(keys)[:20]
        summary["api_error_present"] = bool(parsed.get("error"))
        if expected:
            summary["expected_key"] = expected
            summary["expected_key_present"] = expected in parsed
            if expected in parsed:
                summary["expected_value_type"] = type(parsed[expected]).__name__
    elif isinstance(parsed, list):
        summary["json_type"] = "list"
    else:
        summary["json_type"] = "unrecognized_or_non_json"
        summary["html_script_present"] = "<script" in body.lower()
        summary["html_novel_marker_present"] = bool(re.search(r"\bnovel\s*:\s*\{", body))
        summary["html_login_or_challenge_marker"] = any(
            token in body.lower() for token in ("cloudflare", "cf-chl", "login", "sign in")
        )
    return summary


def make_app_api_class(base_class: type, *, profile: str = "pixev", accept_language: str = "zh-CN") -> type:
    """Subclass PixivPy3 without changing its OAuth, request transport or API methods.

    Legacy mode records identical diagnostics without changing outgoing headers;
    pixev mode injects Android client fields through the *headers argument* of
    no_auth_requests_call, because merely setting `additional_headers` gets
    overwritten by PixivPy3's hard-coded iOS defaults.
    """
    if profile not in {"legacy", "pixev"}:
        raise ValueError("PIXIV_APP_API_PROFILE must be legacy or pixev")

    class CompatiblePixivAPI(base_class):
        app_api_profile = profile
        last_response_diagnostic: dict[str, Any] | None = None

        def no_auth_requests_call(self, method: str, url: str, headers: Any = None,
                                  params: Any = None, data: Any = None,
                                  req_auth: bool = True) -> Any:
            outgoing = dict(headers or {})
            if profile == "pixev" and urlparse(url).hostname == "app-api.pixiv.net":
                outgoing.update(pixev_app_headers(self.hash_secret, accept_language))
            response = super().no_auth_requests_call(
                method, url, headers=outgoing, params=params, data=data,
                req_auth=req_auth,
            )
            self.last_response_diagnostic = inspect_response(response, endpoint=url)
            return response

    CompatiblePixivAPI.__name__ = "PixevCompatiblePixivAPI" if profile == "pixev" else "ObservedLegacyPixivAPI"
    return CompatiblePixivAPI
