"""Tiny standards-based EPUB3 writer (stdlib only)."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import mimetypes
import re
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED


PARTS = re.compile(r"(\[(?:newpage|chapter:[^\]]+|pixivimage:[^\]]+|uploadedimage:[^\]]+)\])")
INVALID_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def xhtml_text(text: str, attachments: dict[str, tuple[str, bytes]]) -> str:
    """Preserve Pixiv chapter/page break markers and optional embedded images."""
    out: list[str] = []
    for item in PARTS.split(text):
        if not item:
            continue
        if item == "[newpage]":
            out.append('<hr class="page"/>')
        elif item.startswith("[chapter:") and item.endswith("]"):
            out.append(f"<h2>{escape(INVALID_XML.sub('', item[9:-1]))}</h2>")
        elif item.startswith("[pixivimage:") or item.startswith("[uploadedimage:"):
            typ, value = item[1:-1].split(":", 1)
            ref = f"{typ}:{value}"
            if ref in attachments:
                filename, _ = attachments[ref]
                out.append(f'<p><img src="images/{escape(filename, quote=True)}" alt="{escape(ref, quote=True)}"/></p>')
            elif typ == "pixivimage" and value.isdigit():
                out.append(f'<p><a href="https://www.pixiv.net/artworks/{value}">Embedded image: {value}</a></p>')
            else:
                out.append(f"<p>{escape(ref)}</p>")
        else:
            clean = INVALID_XML.sub("", item)
            for line in clean.splitlines():
                out.append(f"<p>{escape(line) if line.strip() else '&#160;'}</p>")
    return "\n".join(out)


def build_epub(path: Path, title: str, author: str, text: str,
               attachments: dict[str, tuple[str, bytes]] | None = None) -> None:
    """Create a single-chapter EPUB3. Attachments: marker -> (filename, bytes)."""
    attachments = attachments or {}
    title = INVALID_XML.sub("", title)
    author = INVALID_XML.sub("", author)
    ident = f"urn:uuid:{uuid4()}"
    dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_images: list[str] = []
    for i, (_, (name, _)) in enumerate(attachments.items()):
        mime = mimetypes.guess_type(name)[0] or "image/jpeg"
        manifest_images.append(f'<item id="img{i}" href="images/{escape(name, quote=True)}" media-type="{mime}"/>')
    body = xhtml_text(text, attachments)
    xhtml = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja"><head><title>{escape(title)}</title></head>
<body><h1>{escape(title)}</h1>{body}</body></html>'''
    nav = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja">
<head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="text.xhtml">{escape(title)}</a></li></ol></nav></body></html>'''
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" xml:lang="ja">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">
<dc:identifier id="uid">{ident}</dc:identifier><dc:title>{escape(title)}</dc:title>
<dc:creator>{escape(author)}</dc:creator><dc:language>ja</dc:language>
<meta property="dcterms:modified">{dt}</meta>
</metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="text" href="text.xhtml" media-type="application/xhtml+xml"/>{''.join(manifest_images)}</manifest>
<spine><itemref idref="text"/></spine></package>'''
    container = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        with ZipFile(tmp, "w") as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
            z.writestr("META-INF/container.xml", container, compress_type=ZIP_DEFLATED)
            z.writestr("OEBPS/content.opf", opf, compress_type=ZIP_DEFLATED)
            z.writestr("OEBPS/nav.xhtml", nav, compress_type=ZIP_DEFLATED)
            z.writestr("OEBPS/text.xhtml", xhtml, compress_type=ZIP_DEFLATED)
            written = set()
            for _, (name, binary) in attachments.items():
                if name not in written:
                    z.writestr(f"OEBPS/images/{name}", binary, compress_type=ZIP_STORED)
                    written.add(name)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
