# -*- coding: utf-8 -*-
"""
epub_builder: Module chung để đóng gói EPUB2 đơn giản.
- create_epub(book_url, book_title, author, chapters, fetch_fn, *,
              cover_bytes=None, cover_ext='.jpg', language='vi', creator='Hishiro')
  trong đó:
    - chapters: list[{'title','url'}]
    - fetch_fn(url) -> {'title','content_html','url'}  (site module cung cấp)
"""
from datetime import datetime, timezone
import zipfile, html, time, re
from typing import Callable, List, Dict, Optional

def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)

def _normalize_title(t: Optional[str]) -> str:
    if not t: return "Chương"
    t = t.strip()
    m = re.match(r"^(Chương)\s*(\d+)(\s*[:\-–]?\s*)(.*)$", t, flags=re.I)
    if m:
        name, num, _, rest = m.groups()
        rest = rest.strip()
        return f"{name} {num}: {rest}" if rest else f"{name} {num}"
    return t

def create_epub(book_url: str,
                book_title: str,
                author: str,
                chapters: List[Dict],
                fetch_fn: Callable[[str], Dict],
                *,
                cover_bytes: Optional[bytes]=None,
                cover_ext: str=".jpg",
                language: str="vi",
                creator: str="Hishiro",
                sleep: float=0.12):
    book_title = book_title or "Truyện"
    author = author or "—"

    # Lấy nội dung các chương
    items = []
    for idx, info in enumerate(chapters, 1):
        c = fetch_fn(info["url"])
        if not c.get("title"):
            c["title"] = info.get("title") or f"Chương {idx}"
        c["title"] = _normalize_title(c["title"])
        items.append(c)
        time.sleep(sleep)

    cover_name = "Images/cover"+(cover_ext or ".jpg") if cover_bytes else None

    with zipfile.ZipFile(book_title + ".epub", "w") as z:
        # 1) mimetype
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        # 2) container.xml
        container_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n'
            '</container>'
        ).encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)

        # 3) Styles
        style_css = (
            "body{font-family:serif;line-height:1.6} "
            "img{max-width:100%;height:auto} "
            "h1{font-size:1.4em;margin:0 0 .6em} "
            "p{margin:.5em 0}"
        )
        _epub_write(z, "OEBPS/Styles/style.css", style_css.encode("utf-8"))

        # 4) Text/*.xhtml + manifest/spine/navpoints
        manifest_items = []
        spine_items = []
        navpoints = []
        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"\n'
                ' "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
                '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
                '<meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8"/>\n'
                '<link rel="stylesheet" type="text/css" href="../Styles/style.css"/>\n'
                f"<title>{html.escape(c['title'])}</title>\n</head>\n<body>\n"
                f"<h1>{html.escape(c['title'])}</h1>\n"
                f"{c.get('content_html') or '<p>(Không có nội dung)</p>'}\n"
                "</body></html>"
            ).encode("utf-8")
            _epub_write(z, "OEBPS/" + fn, xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            navpoints.append(f'<navPoint id="nav{i}" playOrder="{i}"><navLabel><text>{html.escape(c["title"])}</text></navLabel><content src="{fn}"/></navPoint>')

        # 5) Cover (nếu có)
        manifest_cover = ""
        meta_cover = ""
        if cover_bytes:
            _epub_write(z, "OEBPS/"+cover_name, cover_bytes)
            ctype = "image/png" if cover_ext.lower().endswith(".png") else "image/jpeg"
            manifest_cover = f'<item id="cover" href="{cover_name}" media-type="{ctype}" properties="cover-image"/>'
            meta_cover = '<meta name="cover" content="cover"/>'

        # 6) content.opf
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        content_opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package version="2.0" unique-identifier="BookId" xmlns="http://www.idpf.org/2007/opf">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'    <dc:title>{html.escape(book_title)}</dc:title>\n'
            f'    <dc:creator>{html.escape(creator)}</dc:creator>\n'
            f'    <dc:language>{language}</dc:language>\n'
            f'    <dc:date>{now}</dc:date>\n'
            f'    <dc:publisher>{html.escape(author)}</dc:publisher>\n'
            f'    <dc:source>{html.escape(book_url or "")}</dc:source>\n'
            f'    {meta_cover}\n'
            '  </metadata>\n'
            '  <manifest>\n'
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
            '    <item id="css" href="Styles/style.css" media-type="text/css"/>\n'
            f'    {manifest_cover}\n'
            f'    {"".join(manifest_items)}\n'
            '  </manifest>\n'
            '  <spine toc="ncx">\n'
            f'    {"".join(spine_items)}\n'
            '  </spine>\n'
            '</package>'
        ).encode("utf-8")
        _epub_write(z, "OEBPS/content.opf", content_opf)

        # 7) toc.ncx
        toc = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            '  <head><meta name="dtb:uid" content="id"/></head>\n'
            f'  <docTitle><text>{html.escape(book_title)}</text></docTitle>\n'
            f'  <navMap>{"".join(navpoints)}</navMap>\n'
            '</ncx>'
        ).encode("utf-8")
        _epub_write(z, "OEBPS/toc.ncx", toc)
