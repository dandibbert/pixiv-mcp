"""Bounded, resumable-by-skipping Pixiv downloads and in-memory job manager."""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid

import requests

from .engine import PixivEngine, field, positive_int, to_dict
from .advanced import SearchOptions
from .epub import build_epub

LOG = logging.getLogger(__name__)
PATH_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
EXT_WHITELIST = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip"}


def safe_name(name: Any, limit: int = 90) -> str:
    cleaned = PATH_UNSAFE.sub("_", str(name or "untitled"))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:limit].strip(" .") or "untitled")


def atomic_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def media_url_ok(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return (u.scheme == "https" and
            (host == "pximg.net" or host.endswith(".pximg.net")) and
            not u.username and not u.password)


def ext_from_url(url: str) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    return ext if ext in EXT_WHITELIST else ".jpg"


def original_urls(illust: Any) -> list[str]:
    """Return every page, never silently substitute one thumbnail for a manga."""
    if field(illust, "type") == "ugoira":
        return []
    pages = field(illust, "meta_pages", []) or []
    if pages:
        output = [field(field(p, "image_urls", {}), "original") for p in pages]
        if not all(isinstance(u, str) and u for u in output):
            raise RuntimeError("Pixiv did not return original image URL for every page")
        return output
    count = int(field(illust, "page_count", 1) or 1)
    if count > 1:
        raise RuntimeError("Missing meta_pages for multi-page manga; refusing incomplete download")
    single = field(field(illust, "meta_single_page", {}), "original_image_url")
    if not single:
        raise RuntimeError("Pixiv did not return an original image URL")
    return [single]


class PixivDownloader:
    def __init__(self, engine: PixivEngine, root: Path, *, proxy: str = "", max_media_bytes: int = 250_000_000):
        self.engine = engine
        self.root = Path(root).expanduser().resolve()
        self.proxy = proxy
        self.max_media_bytes = max_media_bytes
        self.session = requests.Session()
        self.session.headers.update({"Referer": "https://www.pixiv.net/", "User-Agent": "Mozilla/5.0"})
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def fetch_bytes(self, url: str, max_bytes: int | None = None) -> bytes:
        if not media_url_ok(url):
            raise ValueError("Refusing non-Pixiv image URL")
        max_bytes = max_bytes or self.max_media_bytes
        # Never redirect to arbitrary hosts or internal URLs.
        response = self.session.get(url, timeout=(15, 90), stream=True, allow_redirects=False)
        try:
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                raise RuntimeError("Pixiv CDN redirected; refusing unsafe redirection")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=128 * 1024):
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise RuntimeError("Media exceeded configured download size cap")
            return bytes(content)
        finally:
            response.close()

    def download_binary(self, url: str, path: Path) -> str:
        if path.exists() and path.stat().st_size > 0:
            return "skipped"
        payload = self.fetch_bytes(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        try:
            tmp.write_bytes(payload)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        return "downloaded"

    def _directory(self, kind: str, user_id: str, user_name: str, work_id: int, title: str) -> Path:
        return self.root / kind / f"{safe_name(user_id, 24)} - {safe_name(user_name, 60)}" / f"{work_id} - {safe_name(title)}"

    def save_artwork(self, work_id: int) -> dict[str, Any]:
        work_id = positive_int(work_id, "work_id")
        record = self.engine.client.call("illust_detail", work_id)
        illust = field(record, "illust", record)
        typ = field(illust, "type", "illust")
        if typ not in {"illust", "manga", "ugoira"}:
            raise RuntimeError(f"Unsupported artwork type: {typ}")
        user = field(illust, "user", {})
        title = str(field(illust, "title", "untitled"))
        folder = self._directory(typ, str(field(user, "id", "unknown")), str(field(user, "name", "unknown")), work_id, title)
        urls = original_urls(illust)
        if typ == "ugoira":
            meta = self.engine.client.call("ugoira_metadata", work_id)
            data = field(meta, "ugoira_metadata", {})
            zip_urls = field(data, "zip_urls", {})
            url = field(zip_urls, "original") or field(zip_urls, "medium")
            if not url:
                raise RuntimeError("Missing ugoira ZIP URL")
            urls = [url]
            folder.mkdir(parents=True, exist_ok=True)
            atomic_text(folder / "ugoira-frames.json", json.dumps(field(data, "frames", []), ensure_ascii=False, indent=2))
        if not urls:
            raise RuntimeError("Pixiv returned no media URLs")
        files = []
        for idx, url in enumerate(urls):
            suffix = ".zip" if typ == "ugoira" else ext_from_url(url)
            filepath = folder / f"{work_id}_p{idx}{suffix}"
            status = self.download_binary(url, filepath)
            files.append({"path": str(filepath), "status": status})
        metadata = {"id": work_id, "type": typ, "title": title,
                    "user_id": str(field(user, "id", "")), "user_name": str(field(user, "name", "")),
                    "page_count": len(urls), "pixiv_url": f"https://www.pixiv.net/artworks/{work_id}"}
        atomic_text(folder / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        return {"id": work_id, "kind": typ, "folder": str(folder), "files": files}

    def _novel_images(self, web: Any, folder: Path, limit: int = 25) -> dict[str, tuple[str, bytes]]:
        """Fetch embedded images when a Pixiv webview response provides CDN URLs."""
        entries: list[tuple[str, Any]] = []
        for kind, mapping in (("uploadedimage", field(web, "images", {}) or {}),
                              ("pixivimage", field(web, "illusts", {}) or {})):
            if not isinstance(mapping, dict):
                continue
            entries.extend((f"{kind}:{k}", obj) for k, obj in mapping.items())
        assets: dict[str, tuple[str, bytes]] = {}
        for idx, (ref, obj) in enumerate(entries[:limit]):
            payload = field(obj, "illust", obj)
            images = field(payload, "images", payload)
            urls = field(images, "urls", field(images, "image_urls", images))
            url = field(urls, "original") or field(images, "original")
            if not url or not media_url_ok(url):
                continue
            ext = ext_from_url(url)
            name = f"asset_{idx:03d}{ext}"
            path = folder / "assets" / name
            if path.exists() and path.stat().st_size > 0:
                binary = path.read_bytes()
            else:
                binary = self.fetch_bytes(url, max_bytes=30_000_000)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".part")
                try:
                    tmp.write_bytes(binary)
                    tmp.replace(path)
                finally:
                    tmp.unlink(missing_ok=True)
            assets[ref] = (name, binary)
        return assets

    def save_novel(self, novel_id: int, fmt: str = "both", *, include_images: bool = True) -> dict[str, Any]:
        novel_id = positive_int(novel_id, "novel_id")
        if fmt not in {"txt", "epub", "both"}:
            raise ValueError("format must be txt, epub or both")
        detail = self.engine.client.call("novel_detail", novel_id)
        web = self.engine.novel_webview(novel_id)
        novel_text = field(web, "text")
        if not isinstance(novel_text, str):
            raise RuntimeError("Pixiv novel text is not available")
        user = field(detail, "user", {})
        title = str(field(detail, "title", field(web, "title", "untitled")))
        author = str(field(user, "name", "unknown"))
        folder = self._directory("novels", str(field(user, "id", "unknown")), author, novel_id, title)
        stem = f"{novel_id} - {safe_name(title)}"
        files = []
        if fmt in {"txt", "both"}:
            path = folder / f"{stem}.txt"
            status = "skipped" if path.exists() and path.stat().st_size > 0 else "downloaded"
            if status == "downloaded":
                atomic_text(path, novel_text)
            files.append({"path": str(path), "status": status})
        if fmt in {"epub", "both"}:
            path = folder / f"{stem}.epub"
            status = "skipped" if path.exists() and path.stat().st_size > 0 else "downloaded"
            if status == "downloaded":
                attachments = self._novel_images(web, folder) if include_images else {}
                build_epub(path, title, author, novel_text, attachments)
            files.append({"path": str(path), "status": status})
        meta = {"id": novel_id, "title": title, "author": author,
                "user_id": str(field(user, "id", "")), "length": len(novel_text),
                "pixiv_url": f"https://www.pixiv.net/novel/show.php?id={novel_id}"}
        atomic_text(folder / "metadata.json", json.dumps(meta, ensure_ascii=False, indent=2))
        return {"id": novel_id, "kind": "novel", "folder": str(folder), "files": files}


@dataclass
class Job:
    id: str
    source: str
    kind: str
    state: str = "queued"
    created_at: str = dataclass_field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    total: int = 0
    finished: int = 0
    failed: int = 0
    skipped: int = 0
    last_work: int | None = None
    failures: list[dict[str, Any]] = dataclass_field(default_factory=list)
    outputs: list[str] = dataclass_field(default_factory=list)
    canceled: threading.Event = dataclass_field(default_factory=threading.Event, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {"job_id": self.id, "source": self.source, "kind": self.kind, "state": self.state,
                "created_at": self.created_at, "total": self.total, "finished": self.finished,
                "failed": self.failed, "skipped": self.skipped, "last_work": self.last_work,
                "failures": self.failures[-30:], "output_folders": self.outputs[-30:]}


class JobManager:
    def __init__(self, engine: PixivEngine, downloader: PixivDownloader, *, max_batch: int = 300):
        self.engine = engine
        self.downloader = downloader
        self.max_batch = max(1, min(1000, int(max_batch)))
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.semaphore = threading.Semaphore(1)  # One bulk job actively downloading at a time.

    def start(self, *, source: str, kind: str, ids: list[int] | None = None,
              user_id: int | None = None, query: str | None = None, series_id: int | None = None,
              limit: int = 100, restrict: str = "public", fmt: str = "both",
              options: SearchOptions | None = None) -> dict[str, Any]:
        options = options or SearchOptions()
        if source not in {"ids", "user", "bookmarks", "search", "series"}:
            raise ValueError("source must be ids, user, bookmarks, search, series")
        if kind not in {"illust", "manga", "novel", "all"}:
            raise ValueError("kind must be illust, manga, novel, all")
        if fmt not in {"txt", "epub", "both"}:
            raise ValueError("format must be txt, epub or both")
        if restrict not in {"public", "private"}:
            raise ValueError("restrict must be public or private")
        if source == "ids" and options != SearchOptions():
            raise ValueError("source=ids downloads the explicit IDs unchanged; filters are not applied")
        if source not in {"ids", "search"}:
            # Reject strict search-only options before starting an asynchronous
            # task, instead of reporting a surprising background failure.
            from .advanced import UPSTREAM_SORTS, PREVIEW_SORTS
            if (options.lang is not None or options.is_replaceable_only is not None or
                options.include_potential_violation_works is not None or
                options.include_translated_tag_results is not None or
                options.merge_plain_keyword_results is not None or
                options.include_likes or
                options.search_target != "partial_match_for_tags" or
                options.sort in ((UPSTREAM_SORTS - {"date_desc"}) | PREVIEW_SORTS)):
                raise ValueError("These API filters require source=search, not a user/bookmarks/series listing")
        if source == "ids":
            if not ids or not isinstance(ids, list):
                raise ValueError("ids requires nonempty list of Pixiv work IDs")
            if kind == "all":
                raise ValueError("With ids, select an explicit kind; IDs do not encode artwork vs novel")
            cleaned = list(dict.fromkeys(positive_int(v, "work ID") for v in ids))
            if len(cleaned) > self.max_batch:
                raise ValueError(f"Too many IDs: maximum {self.max_batch} per job")
        else:
            cleaned = []
            limit = positive_int(limit, "limit")
            if limit > self.max_batch:
                raise ValueError(f"limit exceeds {self.max_batch}; start another bounded job")
            if source in {"user", "bookmarks"} and user_id is not None:
                positive_int(user_id, "user_id")
            if source == "search":
                if not query:
                    raise ValueError("query required")
                options.validate_kind(kind)
            if source == "series":
                positive_int(series_id, "series_id")
                if kind not in {"novel", "all"}:
                    raise ValueError("series currently supports novel series only")
        job = Job(id=uuid.uuid4().hex[:12], source=source, kind=kind)
        with self.lock:
            if len(self.jobs) >= 200:
                # Retain active jobs, prune completed jobs only.
                for old_id, old in list(self.jobs.items()):
                    if old.state in {"done", "failed", "canceled", "partial"}:
                        del self.jobs[old_id]
                        if len(self.jobs) < 180:
                            break
            self.jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(job, cleaned, user_id, query, series_id, limit, restrict, fmt, options),
                                  daemon=True, name=f"pixiv-job-{job.id}")
        thread.start()
        return job.snapshot()

    def _safe_error(self, exc: Exception) -> str:
        msg = str(exc)
        for secret in (self.engine.client.refresh_token, os.getenv("PIXIV_REFRESH_TOKEN", "")):
            if secret and len(secret) >= 8:
                msg = msg.replace(secret, "[REDACTED]")
        return msg[:280]

    def _run(self, job: Job, ids: list[int], user_id: int | None, query: str | None,
             series_id: int | None, limit: int, restrict: str, fmt: str, options: SearchOptions) -> None:
        try:
            with self.semaphore:
                if job.canceled.is_set():
                    job.state = "canceled"
                    return
                job.state = "listing"
                if job.source == "ids":
                    works = [{"id": wid, "kind": job.kind} for wid in ids]
                else:
                    works = self.engine.list_works(source=job.source, kind=job.kind, user_id=user_id,
                                                   query=query, series_id=series_id, limit=limit, restrict=restrict,
                                                   options=options)
                job.total = len(works)
                job.state = "downloading"
                for item in works:
                    if job.canceled.is_set():
                        job.state = "canceled"
                        return
                    work_id = item["id"]
                    job.last_work = work_id
                    try:
                        if item["kind"] == "novel":
                            result = self.downloader.save_novel(work_id, fmt)
                        else:
                            result = self.downloader.save_artwork(work_id)
                        job.finished += 1
                        if all(f["status"] == "skipped" for f in result["files"]):
                            job.skipped += 1
                        if result["folder"] not in job.outputs:
                            job.outputs.append(result["folder"])
                    except Exception as exc:
                        job.failed += 1
                        job.failures.append({"id": work_id, "kind": item["kind"], "error": self._safe_error(exc)})
                        LOG.warning("Pixiv download failed: %s %s", work_id, type(exc).__name__)
                job.state = "partial" if job.failed else "done"
        except Exception as exc:
            job.failed += 1
            job.failures.append({"error": self._safe_error(exc)})
            job.state = "failed"
            LOG.error("Pixiv job failed (%s)", type(exc).__name__)

    def status(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ValueError("Job not found; in-memory jobs are cleared when MCP server exits")
            return job.snapshot()

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ValueError("Job not found")
            job.canceled.set()
            return job.snapshot()
