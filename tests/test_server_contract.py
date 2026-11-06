"""Isolated MCP registration and wrapper contract without PyPI/network access.

The real MCP SDK must still be installed and verified before claiming live
transport compatibility; this test only validates our tool entrypoints.
"""
import importlib
import sys
import types

from pixiv_mcp_library.engine import PixivClient, PixivEngine


def _import_with_stub(monkeypatch):
    class FakeFastMCP:
        def __init__(self, name, instructions):
            self.name = name
            self.instructions = instructions
            self.tools = {}

        def tool(self):
            def decorate(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorate

    root = types.ModuleType('mcp')
    server = types.ModuleType('mcp.server')
    fastmcp = types.ModuleType('mcp.server.fastmcp')
    fastmcp.FastMCP = FakeFastMCP
    with monkeypatch.context() as patcher:
        patcher.setitem(sys.modules, 'mcp', root)
        patcher.setitem(sys.modules, 'mcp.server', server)
        patcher.setitem(sys.modules, 'mcp.server.fastmcp', fastmcp)
        sys.modules.pop('pixiv_mcp_library.server', None)
        module = importlib.import_module('pixiv_mcp_library.server')
    return module


def test_stub_mcp_tool_registration_and_docs(monkeypatch):
    server = _import_with_stub(monkeypatch)
    expected = {
        'pixiv_auth_status', 'pixiv_diagnose_api', 'pixiv_search_capabilities', 'pixiv_search_advanced',
        'pixiv_list_works', 'pixiv_work_detail', 'pixiv_read_novel',
        'pixiv_start_download', 'pixiv_download_status', 'pixiv_cancel_download',
    }
    assert set(server.MCP.tools) == expected
    assert 'bookmark_num_min' in server.pixiv_search_capabilities()['native_pixiv_filters']['bookmark_range']
    assert 'popular_desc' in server.pixiv_search_capabilities()['native_pixiv_filters']['sort']['novel']
    assert 'popular_preview' in server.pixiv_search_capabilities()['native_pixiv_filters']['sort']['non_premium_preview']


def test_advanced_search_tool_maps_filters_to_engine(monkeypatch):
    server = _import_with_stub(monkeypatch)

    class Engine:
        def search_advanced(self, **kwargs):
            assert kwargs['query'] == '五悠'
            assert kwargs['kind'] == 'manga'
            opts = kwargs['options']
            assert opts.search_target == 'exact_match_for_tags'
            assert opts.min_bookmarks == 250
            assert opts.include_tags == ('五悠',)
            assert opts.rating == 'safe'
            assert opts.exclude_tags == ('AI',)
            return {'items': [], 'next_cursor': None}

    monkeypatch.setattr(server, 'service', lambda: (None, Engine(), None))
    assert server.pixiv_search_advanced(query='五悠', kind='manga',
        search_target='exact_match_for_tags', min_bookmarks=250, include_tags=['五悠'],
        exclude_tags=['AI'], rating='safe') == {'items': [], 'next_cursor': None}


def test_novel_api_params_exposed_in_mcp_wrapper(monkeypatch):
    server = _import_with_stub(monkeypatch)

    class Engine:
        def search_advanced(self, **kwargs):
            opts = kwargs['options']
            assert kwargs['kind'] == 'novel'
            assert opts.lang == 'zh-CN'
            assert opts.is_replaceable_only is True
            assert opts.include_potential_violation_works is False
            assert opts.merge_plain_keyword_results is False
            assert opts.include_translated_tag_results is True
            assert opts.min_text_length == 4000
            assert opts.ai_mode == 'exclude'
            return {'items': []}

    monkeypatch.setattr(server, 'service', lambda: (None, Engine(), None))
    server.pixiv_search_advanced(
        query='虎伏', kind='novel', lang='zh-CN', is_replaceable_only=True,
        include_potential_violation_works=False, merge_plain_keyword_results=False,
        include_translated_tag_results=True, min_text_length=4000, ai_mode='exclude',
    )


def test_bulk_tool_passes_same_filters_to_manager(monkeypatch):
    server = _import_with_stub(monkeypatch)

    class Manager:
        def start(self, **kwargs):
            assert kwargs['source'] == 'search'
            assert kwargs['kind'] == 'novel'
            assert kwargs['query'] == '虎伏'
            assert kwargs['options'].min_text_length == 3000
            assert kwargs['options'].sort == 'date_desc'
            assert kwargs['options'].search_target == 'keyword'
            return {'job_id': 'unit-test'}

    monkeypatch.setattr(server, 'service', lambda: (None, None, Manager()))
    result = server.pixiv_start_download(source='search', kind='novel', query='虎伏',
        filters={'min_text_length': 3000, 'sort': 'date_desc', 'search_target': 'keyword'})
    assert result == {'job_id': 'unit-test'}
