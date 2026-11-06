import json
import time
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import pytest

from pixiv_mcp_library.engine import PixivClient, PixivEngine
from pixiv_mcp_library.downloads import PixivDownloader, JobManager, media_url_ok, original_urls, safe_name
from pixiv_mcp_library.epub import build_epub, xhtml_text


class MockAPI:
    user_id = 42

    def auth(self, refresh_token):
        return {"response": {"refresh_token": refresh_token}}

    def illust_detail(self, illust_id):
        return {"illust": {
            "id": illust_id, "type": "manga", "title": "My Comic!",
            "user": {"id": 42, "name": "Example Artist"}, "page_count": 3,
            "meta_pages": [{"image_urls": {"original": f"https://i.pximg.net/img/{illust_id}_p{n}.jpg"}}
                           for n in range(3)],
        }}

    def novel_detail(self, novel_id):
        return {"id": novel_id, "title": "海辺の物語", "user": {"id": 42, "name": "Test Author"}}

    def webview_novel(self, novel_id):
        return {"id": novel_id, "title": "海辺の物語",
                "text": "[chapter:第一章]\nこんにちは\n[newpage]\n世界", "images": {}}


def fixture_downloader(tmp_path):
    client = PixivClient("secret", api=MockAPI(), delay=0)
    downloader = PixivDownloader(PixivEngine(client), tmp_path)
    downloader.fetch_bytes = lambda url, max_bytes=None: b"FAKEBINARY"
    return downloader


def test_sanitize_and_media_url():
    assert safe_name('../com\nfoo') == "_com_foo"
    assert media_url_ok("https://i.pximg.net/file.jpg") is True
    assert media_url_ok("http://i.pximg.net/file.jpg") is False
    assert media_url_ok("https://pixiv.net.evil.com/img") is False
    assert media_url_ok("https://127.0.0.1/resource") is False


def test_refuse_incomplete_manga():
    with pytest.raises(RuntimeError):
        original_urls({"type": "manga", "page_count": 3, "meta_pages": []})


def test_multi_page_download_and_skip(tmp_path):
    downloader = fixture_downloader(tmp_path)
    result = downloader.save_artwork(777)
    assert len(result["files"]) == 3
    assert all(x["status"] == "downloaded" for x in result["files"])
    assert all(Path(x["path"]).exists() for x in result["files"])
    assert all(x["status"] == "skipped" for x in downloader.save_artwork(777)["files"])


def test_epub_valid_xml_and_assets(tmp_path):
    path = tmp_path / "book.epub"
    build_epub(path, "A & B", "One <Two>",
               "[chapter:First]Hi\n[uploadedimage:42]\n[newpage]Next",
               {"uploadedimage:42": ("asset.jpg", b"FAKEIMG")})
    with ZipFile(path) as z:
        assert z.namelist()[0] == "mimetype"
        assert z.read("mimetype") == b"application/epub+zip"
        assert z.read("OEBPS/images/asset.jpg") == b"FAKEIMG"
        for name in ("OEBPS/text.xhtml", "OEBPS/nav.xhtml", "OEBPS/content.opf"):
            ET.fromstring(z.read(name))
        assert b'images/asset.jpg' in z.read("OEBPS/text.xhtml")
        assert b'A &amp; B' in z.read("OEBPS/text.xhtml")


def test_save_novel_both_formats(tmp_path):
    downloader = fixture_downloader(tmp_path)
    result = downloader.save_novel(888)
    assert {Path(x["path"]).suffix for x in result["files"]} == {".txt", ".epub"}
    assert all(Path(x["path"]).exists() for x in result["files"])
    assert all(x["status"] == "skipped" for x in downloader.save_novel(888)["files"])


def test_job_ids_and_status(tmp_path):
    downloader = fixture_downloader(tmp_path)
    manager = JobManager(downloader.engine, downloader, max_batch=3)
    result = manager.start(source="ids", kind="manga", ids=[1, 2, 2])
    assert result["state"] in {"queued", "listing", "downloading", "done"}
    for _ in range(50):
        status = manager.status(result["job_id"])
        if status["state"] in {"done", "partial", "failed", "canceled"}:
            break
        time.sleep(.02)
    assert status["state"] == "done"
    assert status["total"] == 2 and status["finished"] == 2
    with pytest.raises(ValueError):
        manager.start(source="ids", kind="all", ids=[1])
    with pytest.raises(ValueError):
        manager.start(source="ids", kind="novel", ids=[1, 2, 3, 4])
