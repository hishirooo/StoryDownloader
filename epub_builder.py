# -*- coding: utf-8 -*-
"""
EPUB builder:
- Mặc định EPUB3 (near-kepub): content.opf ở root, nav.xhtml, titlepage.xhtml
- EPUB2 (tuỳ chọn): OEBPS/, toc.ncx, cover.xhtml + guide (Kobo-friendly)
- KEPUB mode: EPUB3 + kobo.js (nhúng vào titlepage + mọi chương)

Tham số chính:
- cover_path (ưu tiên), cover_bytes/cover_ext (fallback)
- with_kobo_js, kobo_js_path
- out_filename: đặt tên file .epub/.kepub.epub (giữ nguyên dấu)
"""

import os, re, html, zipfile
from datetime import datetime, timezone

def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)

def _write_common_css(zf, opf_dir):
    css = (
        "body{font-family:serif;line-height:1.6}"
        "img{max-width:100%;height:auto}"
        "h1{font-size:1.4em;margin:0 0 .6em}"
        "p{margin:.6em 0}"
        ".center{text-align:center}"
    )
    _epub_write(zf, f"{opf_dir}Styles/style.css", css.encode("utf-8"))

def _write_mimetype_and_container(zf, opf_path):
    _epub_write(zf, "mimetype", b"application/epub+zip", compress=False)
    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        f'<rootfiles><rootfile full-path="{opf_path}" media-type="application/oebps-package+xml"/></rootfiles>'
        '</container>'
    ).encode("utf-8")
    _epub_write(zf, "META-INF/container.xml", container_xml)

def _cover_from_path(cover_path: str):
    if cover_path and os.path.isfile(cover_path):
        with open(cover_path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(cover_path)[1].lower()
        if ext in (".jpg", ".jpeg", ".png"):
            return data, ext
    return None, None

def _read_bytes(path: str):
    with open(path, "rb") as f:
        return f.read()

def create_epub(
    *,
    book_url: str,
    book_title: str,
    author: str,
    chapters: list,          # [{'title','url'}, ...]
    fetch_fn,                # callable(url)->{'title','content_html'}
    out_dir: str,
    cover_path: str = None,
    cover_bytes: bytes = None,
    cover_ext: str = ".jpg",
    epub_version: int = 3,   # 3 = EPUB3 (mặc định), 2 = EPUB2
    language: str = "vi",
    creator: str = "Hishiro",
    with_kobo_js: bool = False,
    kobo_js_path: str = None,
    kobo_js_bytes: bytes = None,
    out_filename: str = None,
):
    os.makedirs(out_dir, exist_ok=True)
    book_title = book_title or "Truyện"
    author = author or "—"

    # Thu nội dung chương
    items = []
    for idx, info in enumerate(chapters, 1):
        c = fetch_fn(info["url"])
        if not c.get("title"):
            c["title"] = info.get("title") or f"Chương {idx}"
        items.append(c)

    # Cover
    cov_bytes, cov_ext = _cover_from_path(cover_path)
    if not cov_bytes and cover_bytes:
        cov_bytes, cov_ext = cover_bytes, (cover_ext or ".jpg")
    if not cov_ext:
        cov_ext = ".jpg"

    # File đích
    if not out_filename:
        out_filename = "book.kepub.epub" if with_kobo_js else "book.epub"
    out_epub = os.path.join(out_dir, out_filename)

    # Vị trí OPF
    if epub_version == 2:
        opf_dir = "OEBPS/"
        opf_path = "OEBPS/content.opf"
    else:
        opf_dir = ""
        opf_path = "content.opf"

    # kobo.js
    js_bytes = None
    if with_kobo_js:
        if kobo_js_path and os.path.isfile(kobo_js_path):
            js_bytes = _read_bytes(kobo_js_path)
        elif kobo_js_bytes:
            js_bytes = kobo_js_bytes

    with zipfile.ZipFile(out_epub, "w") as z:
        _write_mimetype_and_container(z, opf_path)
        _write_common_css(z, opf_dir)

        # Cover image
        cover_item = ""
        has_cover = cov_bytes is not None
        if has_cover:
            _epub_write(z, f"{opf_dir}Images/cover{cov_ext}", cov_bytes)
            mt = "image/png" if cov_ext == ".png" else "image/jpeg"
            cover_item = f'<item id="cover-image" href="Images/cover{cov_ext}" media-type="{mt}"/>'

        manifest_items = []
        spine_items = []
        guide_ref = ""

        # Title/Cover page
        if epub_version == 2:
            if has_cover:
                cover_xhtml = (
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<!DOCTYPE html>'
                    f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">'
                    '<head>'
                    f'<title>{html.escape(book_title)} - Cover</title>'
                    '<meta charset="utf-8"/>'
                    '<link href="../Styles/style.css" rel="stylesheet" type="text/css"/>'
                    '</head>'
                    '<body><div class="center">'
                    f'<img src="../Images/cover{cov_ext}" alt="cover"/>'
                    '</div></body></html>'
                ).encode("utf-8")
                _epub_write(z, f"{opf_dir}Text/cover.xhtml", cover_xhtml)
                manifest_items.append('<item id="cover-page" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
                spine_items.append('<itemref idref="cover-page" linear="no"/>')
                guide_ref = '<reference type="cover" title="Cover" href="Text/cover.xhtml"/>'
        else:
            if has_cover:
                script_tag = ''
                if with_kobo_js and js_bytes:
                    script_tag = '<script type="text/javascript" src="kobo.js"></script>'
                title_xhtml = (
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<!DOCTYPE html>'
                    f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">'
                    '<head>'
                    f'<title>{html.escape(book_title)}</title>'
                    '<meta charset="utf-8"/>'
                    '<link href="Styles/style.css" rel="stylesheet" type="text/css"/>'
                    f'{script_tag}'
                    '</head>'
                    '<body><div class="center">'
                    f'<img src="Images/cover{cov_ext}" alt="cover"/>'
                    '</div></body></html>'
                ).encode("utf-8")
                _epub_write(z, f"{opf_dir}titlepage.xhtml", title_xhtml)
                manifest_items.append('<item id="titlepage" href="titlepage.xhtml" media-type="application/xhtml+xml"/>')
                spine_items.append('<itemref idref="titlepage" linear="no"/>')

        # kobo.js
        if with_kobo_js and js_bytes and epub_version == 3:
            _epub_write(z, f"{opf_dir}kobo.js", js_bytes)

        # Chapters
        navpoints = []
        nav_li = []
        for i, c in enumerate(items, 1):
            fn_rel = f"Text/chapter_{i:04d}.xhtml"
            fn_zip = f"{opf_dir}{fn_rel}"
            script_tag = ''
            if with_kobo_js and js_bytes and epub_version == 3:
                script_tag = '<script type="text/javascript" src="../kobo.js"></script>'
            xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<!DOCTYPE html>'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">'
                '<head>'
                f'<title>{html.escape(c["title"])}</title>'
                '<meta charset="utf-8"/>'
                f'<link href="../Styles/style.css" rel="stylesheet" type="text/css"/>'
                f'{script_tag}'
                '</head>'
                '<body>'
                f'<h1>{html.escape(c["title"])}</h1>'
                f'<div>{c["content_html"]}</div>'
                '</body></html>'
            ).encode("utf-8")
            _epub_write(z, fn_zip, xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn_rel}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            navpoints.append(
                f'<navPoint id="navPoint-{i}" playOrder="{i}">'
                f'<navLabel><text>{html.escape(c["title"])}</text></navLabel>'
                f'<content src="{fn_rel}"/></navPoint>'
            )
            nav_li.append(f'<li><a href="{fn_rel}">{html.escape(c["title"])}</a></li>')

        # TOC
        if epub_version == 2:
            navpoints_str = "\n    ".join(navpoints)
            toc_ncx = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                '<head>'
                f'<meta name="dtb:uid" content="urn:uuid:{html.escape(book_title)}"/>'
                '<meta name="dtb:depth" content="1"/>'
                '<meta name="dtb:totalPageCount" content="0"/>'
                '<meta name="dtb:maxPageNumber" content="0"/>'
                '</head>'
                f'<docTitle><text>{html.escape(book_title)}</text></docTitle>'
                f'<navMap>{navpoints_str}</navMap>'
                '</ncx>'
            ).encode("utf-8")
            _epub_write(z, f"{opf_dir}toc.ncx", toc_ncx)
        else:
            nav_li_str = "\n      ".join(nav_li)
            nav_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<!DOCTYPE html>'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">'
                '<head>'
                '<meta charset="utf-8"/>'
                '<title>Table of Contents</title>'
                '<link href="Styles/style.css" rel="stylesheet" type="text/css"/>'
                '</head>'
                '<body>'
                '<nav epub:type="toc" id="toc"><h1>Mục lục</h1>'
                f'<ol>{nav_li_str}</ol>'
                '</nav>'
                '</body></html>'
            ).encode("utf-8")
            _epub_write(z, f"{opf_dir}nav.xhtml", nav_xhtml)

        # OPF
        manifest_core = []
        if has_cover:
            manifest_core.append(cover_item)
        if epub_version == 2:
            manifest_core.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
        else:
            manifest_core.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        manifest_core.append('<item id="css" href="Styles/style.css" media-type="text/css"/>')
        if with_kobo_js and js_bytes and epub_version == 3:
            manifest_core.append('<item id="kobojs" href="kobo.js" media-type="text/javascript"/>')

        manifest_str = "\n    ".join(manifest_core + manifest_items)
        spine_str = "\n    ".join(spine_items)
        dt_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta_cover_link = '<meta name="cover" content="cover-image"/>' if has_cover else ""

        if epub_version == 2:
            guide_block = f"<guide>{guide_ref}</guide>" if guide_ref else "<guide/>"
            content_opf = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="2.0">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f'<dc:identifier id="BookID">urn:uuid:{int(datetime.now().timestamp())}</dc:identifier>'
                f'<dc:title>{html.escape(book_title)}</dc:title>'
                f'<dc:creator>{html.escape(author)}</dc:creator>'
                f'<dc:language>{language}</dc:language>'
                f'{meta_cover_link}'
                f'<meta name="creator" content="{html.escape(creator)}"/>'
                f'<meta property="dcterms:modified">{dt_utc}</meta>'
                '</metadata>'
                f'<manifest>{manifest_str}</manifest>'
                f'<spine toc="ncx">{spine_str}</spine>'
                f'{guide_block}'
                '</package>'
            ).encode("utf-8")
        else:
            content_opf = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="3.0">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f'<dc:identifier id="BookID">urn:uuid:{int(datetime.now().timestamp())}</dc:identifier>'
                f'<dc:title>{html.escape(book_title)}</dc:title>'
                f'<dc:creator>{html.escape(author)}</dc:creator>'
                f'<dc:language>{language}</dc:language>'
                f'{meta_cover_link}'
                f'<meta name="creator" content="{html.escape(creator)}"/>'
                f'<meta property="dcterms:modified">{dt_utc}</meta>'
                '</metadata>'
                f'<manifest>{manifest_str}</manifest>'
                f'<spine>{spine_str}</spine>'
                '</package>'
            ).encode("utf-8")

        _epub_write(z, opf_path, content_opf)

    return out_epub
