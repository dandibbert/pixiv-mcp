"""Small, fail-closed HTTP boundary for the personal Pixiv MCP server.

This is **static bearer token** authentication for private deployments, not an
OAuth authorization server. It authenticates GET/POST/DELETE before the MCP ASGI
handler sees the request and leaves the SDK's lifespan and stdio unchanged.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable
from urllib.parse import urlsplit

# No wildcard hosts: a stray Host header must never make a new origin trusted.
_HOST_PATTERN = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::([0-9]{1,5}))?$")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _parse_host(authority: str) -> tuple[str, int | None]:
    if not isinstance(authority, str) or not authority:
        raise ValueError("Invalid HTTP host")
    match = _HOST_PATTERN.fullmatch(authority)
    if not match:
        raise ValueError("Invalid HTTP host")
    name = match.group(1).lower()
    if name.startswith("["):
        try:
            ipaddress.IPv6Address(name[1:-1])
        except ValueError:
            raise ValueError("Invalid IPv6 HTTP host") from None
    elif ".." in name or name.startswith(".") or name.endswith("."):
        raise ValueError("Invalid HTTP host")
    number = match.group(2)
    port = int(number) if number is not None else None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("HTTP host port must be between 1 and 65535")
    return name, port


def _validated_origin(origin: str) -> tuple[str, str, int | None]:
    try:
        split = urlsplit(origin)
        if split.scheme not in ("http", "https") or not split.netloc:
            raise ValueError()
        if split.username or split.password or split.path or split.query or split.fragment:
            raise ValueError()
        # No Unicode spoofing; browsers send the IDNA form of the host.
        raw_host, raw_port = _parse_host(split.netloc)
        return split.scheme, raw_host, raw_port
    except (ValueError, TypeError):
        raise ValueError("Invalid Origin header") from None


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in (value or "").split(",") if x.strip())


@dataclass(frozen=True)
class HttpAuthConfig:
    bearer_token: str = dataclass_field(repr=False)
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        token: str,
        bind_host: str,
        additional_hosts: Iterable[str] = (),
        additional_origins: Iterable[str] = (),
    ) -> "HttpAuthConfig":
        # A generated 32-byte token is ~43 chars; enforce >=32 ASCII chars.
        # Do not strip/modify a token: changed bytes are authentication errors.
        if not isinstance(token, str) or len(token) < 32 or len(token) > 4096:
            raise ValueError("HTTP requires PIXIV_MCP_BEARER_TOKEN (minimum 32 ASCII characters)")
        try:
            token_bytes = token.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("PIXIV_MCP_BEARER_TOKEN must contain only ASCII characters") from None
        if any(b < 33 or b > 126 for b in token_bytes):
            raise ValueError("PIXIV_MCP_BEARER_TOKEN cannot contain spaces or control characters")

        hosts = list(LOCAL_HOSTS)
        configured = [x for x in additional_hosts if x]
        if bind_host not in {"0.0.0.0", "::", "[::]", "127.0.0.1", "localhost", "::1", "[::1]"}:
            configured.append(bind_host)
        for entry in configured:
            _parse_host(entry)  # reject schemes, paths, wildcards, CRLF and invalid ports
            if entry.lower() not in {h.lower() for h in hosts}:
                hosts.append(entry.lower())
        if bind_host in {"0.0.0.0", "::", "[::]"} and not any(
            _parse_host(entry)[0] not in {"127.0.0.1", "localhost", "[::1]"}
            for entry in configured
        ):
            raise ValueError("Wildcard HTTP bind requires a non-loopback Host in PIXIV_MCP_ALLOWED_HOSTS or --allow-host")

        origins = []
        for entry in additional_origins:
            _validated_origin(entry)
            if entry not in origins:
                origins.append(entry)
        return cls(token, tuple(hosts), tuple(origins))

    @classmethod
    def from_environment(
        cls, env: dict[str, str] | Any, *, bind_host: str,
        allowed_hosts: Iterable[str] = (), allowed_origins: Iterable[str] = (),
    ) -> "HttpAuthConfig":
        return cls.build(
            token=env.get("PIXIV_MCP_BEARER_TOKEN", ""),
            bind_host=bind_host,
            additional_hosts=[*_split_csv(env.get("PIXIV_MCP_ALLOWED_HOSTS", "")), *allowed_hosts],
            additional_origins=[*_split_csv(env.get("PIXIV_MCP_ALLOWED_ORIGINS", "")), *allowed_origins],
        )

    def _host_allowed(self, authority: str) -> bool:
        try:
            host, port = _parse_host(authority)
        except ValueError:
            return False
        for allowed in self.allowed_hosts:
            allowed_host, allowed_port = _parse_host(allowed)
            if host == allowed_host and (allowed_port is None or allowed_port == port):
                return True
        return False

    def _origin_allowed(self, origin: str, authority: str) -> bool:
        try:
            scheme, origin_host, origin_port = _validated_origin(origin)
            host, port = _parse_host(authority)
        except ValueError:
            return False
        default_port = 443 if scheme == "https" else 80
        if origin_host == host and (origin_port or default_port) == (port or default_port):
            return True
        return origin in self.allowed_origins

    def transport_security(self) -> Any:
        """MCP SDK v1 rebinding guard; the wrapper performs an exact check too."""
        from mcp.server.transport_security import TransportSecuritySettings

        hosts: set[str] = set()
        origins: set[str] = set(self.allowed_origins)
        for entry in self.allowed_hosts:
            name, port = _parse_host(entry)
            hosts.add(entry)
            if port is None:
                hosts.add(f"{name}:*")
                for scheme in ("http", "https"):
                    origins.update((f"{scheme}://{name}", f"{scheme}://{name}:*"))
            else:
                for scheme in ("http", "https"):
                    origins.add(f"{scheme}://{entry}")
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(hosts),
            allowed_origins=sorted(origins),
        )


class BearerAuthASGI:
    """Pure ASGI gate; does not buffer or consume MCP SSE response bodies.

    Validates host/origin *and* bearer on each HTTP request, including DELETE
    (which can terminate a session). Lifespan messages pass through unchanged.
    """

    def __init__(self, app: Any, config: HttpAuthConfig):
        self.app = app
        self.config = config
        self._token = config.bearer_token.encode("ascii")

    @staticmethod
    async def _reject(send: Any, status: int, error: str) -> None:
        body = json.dumps({"error": error}, separators=(",", ":")).encode("utf-8")
        headers = [(b"content-type", b"application/json"),
                   (b"cache-control", b"no-store"),
                   (b"content-length", str(len(body)).encode("ascii"))]
        if status == 401:
            headers.append((b"www-authenticate", b'Bearer realm="pixiv-mcp"'))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # MCP never uses WebSocket; don't allow an unguarded alternate protocol.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            return
        raw_headers = scope.get("headers", [])
        hosts = [v for k, v in raw_headers if k.lower() == b"host"]
        origins = [v for k, v in raw_headers if k.lower() == b"origin"]
        auth = [v for k, v in raw_headers if k.lower() == b"authorization"]
        if len(hosts) != 1:
            await self._reject(send, 421, "invalid_host")
            return
        try:
            host = hosts[0].decode("ascii")
        except UnicodeDecodeError:
            await self._reject(send, 421, "invalid_host")
            return
        if not self.config._host_allowed(host):
            await self._reject(send, 421, "invalid_host")
            return
        if len(origins) > 1:
            await self._reject(send, 403, "invalid_origin")
            return
        if origins:
            try:
                origin = origins[0].decode("ascii")
            except UnicodeDecodeError:
                await self._reject(send, 403, "invalid_origin")
                return
            if not self.config._origin_allowed(origin, host):
                await self._reject(send, 403, "invalid_origin")
                return
        if len(auth) != 1:
            await self._reject(send, 401, "unauthorized")
            return
        # Only a single header and exactly one Bearer token, no comma-joined
        # values or accidental acceptance of Basic, query tokens or cookies.
        raw = auth[0]
        if not raw.lower().startswith(b"bearer "):
            await self._reject(send, 401, "unauthorized")
            return
        candidate = raw[7:]
        if not hmac.compare_digest(candidate, self._token):
            await self._reject(send, 401, "unauthorized")
            return
        await self.app(scope, receive, send)
