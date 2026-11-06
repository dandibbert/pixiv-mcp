"""HTTP security tests require no Pixiv credentials or MCP installation."""
from __future__ import annotations

import asyncio
import os
import sys
import types

import httpx
import pytest

from pixiv_mcp_library.http_auth import BearerAuthASGI, HttpAuthConfig

TOKEN = 'unit_test_very_long_token_ABCDEFGHIJKLMNOPQRSTUVWXYZ12345'


class FakeApp:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append((scope['type'], scope.get('method'), scope.get('path')))
        if scope['type'] == 'lifespan':
            message = await receive()
            assert message['type'] == 'lifespan.startup'
            await send({'type': 'lifespan.startup.complete'})
            message = await receive()
            assert message['type'] == 'lifespan.shutdown'
            await send({'type': 'lifespan.shutdown.complete'})
        elif scope['type'] == 'http':
            await send({'type':'http.response.start', 'status':200, 'headers':[(b'content-type',b'text/event-stream')]})
            await send({'type':'http.response.body', 'body':b'data: first\n\n', 'more_body':True})
            await send({'type':'http.response.body', 'body':b'data: second\n\n', 'more_body':False})


def _config(host='127.0.0.1', hosts=(), origins=()):
    return HttpAuthConfig.build(token=TOKEN, bind_host=host,
                                additional_hosts=hosts, additional_origins=origins)


def _request(app, method='POST', url='http://127.0.0.1:8765/mcp', headers=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=5) as client:
            return await client.request(method, url, headers=headers)
    return asyncio.run(go())


@pytest.mark.parametrize('method', ['GET', 'POST', 'DELETE', 'PATCH'])
def test_unauthorized_never_reaches_mcp(method):
    inner = FakeApp()
    app = BearerAuthASGI(inner, _config())
    for headers in [None, {'authorization':'Basic abc'}, {'authorization':'Bearer wrong'},
                    {'authorization': f'Bearer {TOKEN}x'}]:
        result = _request(app, method, headers=headers)
        assert result.status_code == 401
        assert result.headers['www-authenticate'].startswith('Bearer ')
        assert result.json() == {'error':'unauthorized'}
        assert result.headers['cache-control'] == 'no-store'
    assert not inner.calls


@pytest.mark.parametrize('method', ['GET','POST','DELETE'])
def test_correct_token_passes_and_streaming_response_is_not_modified(method):
    inner=FakeApp()
    app=BearerAuthASGI(inner, _config())
    result=_request(app, method, headers={'Authorization': f'bEaReR {TOKEN}'})
    assert result.status_code == 200
    assert result.content == b'data: first\n\ndata: second\n\n'
    assert result.headers['content-type'] == 'text/event-stream'
    assert inner.calls == [('http', method, '/mcp')]


def test_duplicate_bearer_headers_are_rejected():
    inner = FakeApp()
    app = BearerAuthASGI(inner, _config())
    h = [('Authorization', f'Bearer {TOKEN}'),('authorization', f'Bearer {TOKEN}')]
    assert _request(app,headers=h).status_code == 401
    assert not inner.calls


def test_query_or_cookie_token_does_not_authenticate():
    inner=FakeApp()
    app=BearerAuthASGI(inner, _config())
    assert _request(app, url=f'http://localhost:8765/mcp?token={TOKEN}').status_code==401
    assert _request(app, headers={'Cookie':f'token={TOKEN}'}).status_code==401
    assert not inner.calls


def test_host_validation_blocks_rebinding_and_host_prefix():
    inner=FakeApp()
    app=BearerAuthASGI(inner, _config())
    headers={'authorization':f'Bearer {TOKEN}'}
    for url in ['http://example.org:8765/mcp','http://localhost.evil.org:8765/mcp',
                'http://127.0.0.1.evil.org:8765/mcp']:
        assert _request(app, url=url, headers=headers).status_code==421
    assert not inner.calls


def test_repeated_host_headers_rejected():
    inner=FakeApp()
    app=BearerAuthASGI(inner, _config())
    result=_request(app, headers=[('host','127.0.0.1:8765'),('host','127.0.0.1:8765'),
                                 ('authorization',f'Bearer {TOKEN}')])
    assert result.status_code == 421
    assert not inner.calls


def test_origin_enforced_and_same_origin_allowed():
    inner=FakeApp()
    app=BearerAuthASGI(inner, _config())
    headers={'authorization':f'Bearer {TOKEN}'}
    assert _request(app, headers={**headers,'origin':'https://hostile.example'}).status_code == 403
    assert _request(app, headers={**headers,'origin':'http://127.0.0.1:8765'}).status_code == 200
    assert _request(app, headers={**headers,'origin':'http://127.0.0.1:9999'}).status_code == 403
    assert len(inner.calls) == 1


def test_cross_origin_whitelist_allows_only_configured_origin():
    inner=FakeApp()
    config=_config(origins=['https://client.example.org'])
    app=BearerAuthASGI(inner, config)
    headers={'authorization':f'Bearer {TOKEN}', 'origin':'https://client.example.org'}
    assert _request(app,headers=headers).status_code == 200
    headers['origin']='https://client.example.org.attacker'
    assert _request(app,headers=headers).status_code == 403
    assert len(inner.calls) == 1


def test_remote_bind_requires_explicit_remote_host():
    with pytest.raises(ValueError, match='Wildcard HTTP bind'):
        _config(host='0.0.0.0')
    config = _config(host='0.0.0.0', hosts=['mcp.example.org', '100.101.102.103'])
    app = BearerAuthASGI(FakeApp(), config)
    h={'Authorization':f'Bearer {TOKEN}'}
    assert _request(app, url='http://mcp.example.org/mcp', headers=h).status_code==200
    assert _request(app, url='http://100.101.102.103:8765/mcp', headers=h).status_code==200
    assert _request(app, url='http://random.example.org:8765/mcp',headers=h).status_code==421


def test_explicit_host_port_constraint():
    config = _config(host='0.0.0.0', hosts=['mcp.example.org:8765'])
    app = BearerAuthASGI(FakeApp(), config)
    h={'Authorization':f'Bearer {TOKEN}'}
    assert _request(app, url='http://mcp.example.org:8765/mcp',headers=h).status_code==200
    assert _request(app, url='http://mcp.example.org:9999/mcp',headers=h).status_code==421


def test_bind_specific_ip_is_allowlisted_without_wildcard():
    config = _config(host='100.101.102.103')
    app = BearerAuthASGI(FakeApp(), config)
    assert _request(app,url='http://100.101.102.103:8765/mcp',
                    headers={'Authorization':f'Bearer {TOKEN}'}).status_code == 200


@pytest.mark.parametrize('invalid', ['', 'x'*31, '密'*40, ' ' * 50, 'x'*45+'\n', 'x'*4097])
def test_bad_secret_rejected_at_startup(invalid):
    with pytest.raises(ValueError,match='PIXIV_MCP_BEARER_TOKEN'):
        HttpAuthConfig.build(token=invalid,bind_host='127.0.0.1')


@pytest.mark.parametrize('invalid', ['*','https://mcp.example.org','bad/foo','abc:70000',
                                     'bad.example.org\nX-Malicious: x','[invalid]', 'evil..org'])
def test_bad_allow_hosts_fail_closed(invalid):
    with pytest.raises(ValueError):
        _config(host='0.0.0.0',hosts=[invalid])


@pytest.mark.parametrize('invalid', ['*','null','file://evil', 'https://user@site.org',
                                     'https://site.org/path', 'https://evil.org\nX-Evil: value'])
def test_bad_origins_fail_closed(invalid):
    with pytest.raises(ValueError):
        _config(origins=[invalid])


def test_environment_variables_and_port_behavior():
    config=HttpAuthConfig.from_environment({
        'PIXIV_MCP_BEARER_TOKEN': TOKEN,
        'PIXIV_MCP_ALLOWED_HOSTS': 'mcp.example.org, 100.101.102.103',
        'PIXIV_MCP_ALLOWED_ORIGINS': 'https://reader.example.org',
    }, bind_host='0.0.0.0')
    assert 'mcp.example.org' in config.allowed_hosts
    assert 'https://reader.example.org' in config.allowed_origins
    assert config._host_allowed('mcp.example.org:1234')
    assert not config._host_allowed('mcp.example.org.evil:1234')
    assert not config._origin_allowed('http://reader.example.org', 'mcp.example.org')


def test_lifespan_preserved_for_streamable_http_sdk():
    inner=FakeApp()
    app=BearerAuthASGI(inner, _config())

    async def go():
        messages=iter([{'type':'lifespan.startup'}, {'type':'lifespan.shutdown'}])
        sent=[]
        async def recv(): return next(messages)
        async def send(message): sent.append(message)
        await app({'type':'lifespan'},recv,send)
        return sent
    result=asyncio.run(go())
    assert result==[{'type':'lifespan.startup.complete'}, {'type':'lifespan.shutdown.complete'}]
    assert len(inner.calls)==1 and inner.calls[0][0]=='lifespan'


def test_transport_security_config_uses_sdk_dns_rebinding_guard(monkeypatch):
    class SDKTransportSettings:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    root = types.ModuleType('mcp')
    server = types.ModuleType('mcp.server')
    sec = types.ModuleType('mcp.server.transport_security')
    sec.TransportSecuritySettings = SDKTransportSettings
    with monkeypatch.context() as m:
        m.setitem(sys.modules,'mcp',root)
        m.setitem(sys.modules,'mcp.server',server)
        m.setitem(sys.modules,'mcp.server.transport_security',sec)
        opts=_config(host='0.0.0.0',hosts=['mcp.example.org']).transport_security()
    assert opts.enable_dns_rebinding_protection
    assert 'mcp.example.org:*' in opts.allowed_hosts
    assert 'https://mcp.example.org:*' in opts.allowed_origins
    assert '127.0.0.1:*' in opts.allowed_hosts


def test_service_auth_config_never_exposes_pixiv_oauth_token():
    config=HttpAuthConfig.from_environment({'PIXIV_MCP_BEARER_TOKEN':TOKEN,
            'PIXIV_REFRESH_TOKEN':'PIXIV_SECRET_DO_NOT_EXPOSE'},bind_host='127.0.0.1')
    assert config.bearer_token==TOKEN
    assert 'PIXIV_SECRET_DO_NOT_EXPOSE' not in repr(config.allowed_hosts)
