# -*- coding: utf-8 -*-
"""
epub_builder: Module chung để đóng gói EPUB2 đơn giản.
PHIÊN BẢN TỐI ƯU: Không tải trùng, hỗ trợ cache từ HTML/dict
"""
from datetime import datetime, timezone
from uuid import uuid4
import zipfile, html, time, re, os, json, sys
from typing import Any, Callable, List, Dict, Optional, Union
from pathlib import Path
from bs4 import BeautifulSoup
from epub_metadata import PUBLISHER, normalize_tags, subject_xml

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
        rest = (rest or "").strip()
        return f"{name} {num}: {rest}" if rest else f"{name} {num}"
    return t

def _safe_fs_name(name: str, maxlen: int = 150) -> str:
    """Làm sạch tên file cho Windows/macOS/Linux (loại ký tự cấm)."""
    s = name or "book"
    s = re.sub(r'[\\/:*?"<>|]+', ' - ', s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    return (s[:maxlen] or "book")

def _safe_print(message: str) -> None:
    try:
        sys.stdout.buffer.write((message + "\n").encode("utf-8", errors="replace"))
    except Exception:
        try:
            sys.stdout.write(message + "\n")
        except Exception:
            pass


def _cover_media_type(ext: str) -> str:
    ext = (ext or ".jpg").lower()
    if ext == ".jpeg":
        ext = ".jpg"
    return {
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/jpeg")


def _xhtml_page(title: str, body_html: str, *, language: str = "vi", css_href: str = "../Styles/style.css") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{html.escape(language)}" lang="{html.escape(language)}">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="{css_href}"/>
</head>
<body>
{body_html}
</body>
</html>
"""


def _first_text(info: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = info.get(key)
        if isinstance(value, (list, tuple, set)):
            value = ", ".join(str(item) for item in value if item)
        if value:
            return re.sub(r"\s+", " ", html.unescape(str(value))).strip()
    return ""


def _intro_html(book_info: Dict[str, Any], *, tags: Optional[Any] = None) -> str:
    title = _first_text(book_info, "title", "name") or "Truyen"
    author = _first_text(book_info, "author", "creator") or "Unknown"
    team = _first_text(book_info, "artist", "team", "translator", "editor")
    status = _first_text(book_info, "status")
    categories = ", ".join(normalize_tags(tags if tags is not None else book_info))
    intro = _first_text(book_info, "intro", "description", "desc", "summary", "synopsis", "introduction")

    lines = [
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="meta"><strong>Tác giả:</strong> {html.escape(author)}</p>',
    ]
    if team:
        lines.append(f'<p class="meta"><strong>Nhóm dịch:</strong> {html.escape(team)}</p>')
    if status:
        lines.append(f'<p class="meta"><strong>Trạng thái:</strong> {html.escape(status)}</p>')
    if categories:
        lines.append(f'<p class="meta"><strong>Thể loại:</strong> {html.escape(categories)}</p>')
    if intro:
        lines.append("<hr/>")
        for raw_line in str(intro).splitlines():
            line = re.sub(r"\s+", " ", html.unescape(raw_line)).strip()
            if line:
                lines.append(f'<p class="intro">{html.escape(line)}</p>')
    return "\n".join(lines)


def _normalize_xhtml_fragment(html_fragment: str) -> str:
    """Chuyển các đoạn HTML sang XHTML hợp lệ cho EPUB."""
    if not html_fragment:
        return html_fragment or ""

    void_tags = [
        "br", "img", "hr", "meta", "link", "input", "source",
        "embed", "param", "area", "base", "col", "command",
        "keygen", "track", "wbr"
    ]
    pattern = re.compile(
        r'<(?P<tag>' + '|'.join(void_tags) + r')(?P<attrs>\s[^>/]*?)?\s*(?<!/)>',
        flags=re.I
    )

    return pattern.sub(lambda m: f"<{m.group('tag')}{m.group('attrs') or ''}/>", html_fragment)


def _extract_content_from_html(html_path: str) -> Dict:
    """Trích xuất title và content_html từ file HTML đã lưu."""
    _safe_print(f"[EPUB] Doc tu HTML cache: {os.path.basename(html_path)}")
    
    with open(html_path, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')
    
    # Lấy title
    title_tag = soup.find('title')
    title = title_tag.get_text(strip=True) if title_tag else None
    if title and ' - ' in title:
        title = title.split(' - ', 1)[0].strip()
    
    # Lấy h1 nếu có
    h1 = soup.find('h1')
    if h1:
        title = h1.get_text(strip=True)
    
    # Lấy content
    content_node = (
        soup.select_one('article') or 
        soup.select_one('.chapter') or 
        soup.select_one('.content') or
        soup.select_one('body')
    )
    
    if content_node:
        # Xóa h1 để không trùng
        for h in content_node.find_all('h1'):
            h.decompose()
        content_html = str(content_node)
    else:
        content_html = "<p>(Không có nội dung)</p>"
    
    return {
        "title": _normalize_title(title),
        "content_html": content_html,
        "url": html_path
    }

def create_epub(book_url: str,
                book_title: str,
                author: str,
                chapters: List[Dict],
                fetch_fn: Optional[Callable[[str], Dict]] = None,
                *,
                cover_bytes: Optional[bytes]=None,
                cover_ext: str=".jpg",
                language: str="vi",
                creator: str="Hishiro",
                publisher: str=PUBLISHER,
                tags: Optional[Any]=None,
                sleep: float=0.12,
                out_epub_path: Optional[str]=None,
                html_cache_dir: Optional[str]=None,
                chapters_data: Optional[List[Dict]]=None,
                book_info: Optional[Dict[str, Any]]=None,
                intro: Optional[str]=None):
    """
    Tạo EPUB từ danh sách chapters.
    
    Tham số:
        chapters: List[Dict] với key 'title' và 'url'
        fetch_fn: Function để tải nội dung nếu cần (không dùng nếu có cache)
        html_cache_dir: Thư mục chứa HTML đã tải (nếu có)
        chapters_data: Danh sách nội dung đã tải sẵn (ưu tiên cao nhất)
    
    Ưu tiên:
        1. chapters_data (dict với title, content_html)
        2. HTML files trong html_cache_dir
        3. Tải mới qua fetch_fn
    """
    book_title = book_title or "Truyện"
    author = author or "–"

    # Thu thập nội dung các chương
    items = []
    
    for idx, info in enumerate(chapters, 1):
        chapter_url = info["url"]
        chapter_title = info.get("title") or f"Chương {idx}"
        
        # ========== ƯU TIÊN 1: Dùng chapters_data nếu có ==========
        if chapters_data and idx <= len(chapters_data):
            c = chapters_data[idx - 1]
            c["title"] = _normalize_title(c["title"])
            items.append(c)
            continue
        
        # ========== ƯU TIÊN 2: Đọc từ HTML cache ==========
        if html_cache_dir and os.path.isdir(html_cache_dir):
            # Tìm file HTML tương ứng
            expected_filename = f"{idx:04d}"
            html_file = None
            
            for fname in os.listdir(html_cache_dir):
                if fname.startswith(expected_filename) and fname.endswith('.html'):
                    html_file = os.path.join(html_cache_dir, fname)
                    break
            
            if html_file and os.path.exists(html_file):
                try:
                    c = _extract_content_from_html(html_file)
                    if not c.get("title"):
                        c["title"] = chapter_title
                    items.append(c)
                    continue
                except Exception as e:
                    _safe_print(f"[WARNING] [{idx:04d}] Loi doc HTML cache: {e}, se tai moi...")
        
        # ========== ƯU TIÊN 3: Tải mới ==========
        if fetch_fn:
            _safe_print(f"[INFO] [{idx:04d}] Dang tai moi tu Internet: {chapter_url}")
            c = fetch_fn(chapter_url)
            if not c.get("title"):
                c["title"] = chapter_title
            c["title"] = _normalize_title(c["title"])
            items.append(c)
            time.sleep(sleep)
        else:
            _safe_print(f"[ERROR] [{idx:04d}] Khong the lay noi dung: Khong co fetch_fn va khong tim thay cache")
            items.append({
                "title": _normalize_title(chapter_title),
                "content_html": "<p>(Không có nội dung)</p>",
                "url": chapter_url
            })

    # Đếm số lần sử dụng cache và tải mới một cách chính xác
    used_cache_count = 0
    fetched_new_count = 0
    for item in items:
        # Giả sử rằng nếu một item có 'url' là một đường dẫn file, nó đã được lấy từ cache.
        if isinstance(item.get("url"), str) and (os.path.exists(item["url"]) or ".html" in item["url"]):
            used_cache_count += 1
        else:
            fetched_new_count += 1

    info_for_intro: Dict[str, Any] = dict(book_info or {})
    subject_source = tags if tags is not None else info_for_intro
    info_for_intro.setdefault("title", book_title)
    info_for_intro.setdefault("author", author)
    info_for_intro.setdefault("url", book_url or "")
    if intro and not _first_text(info_for_intro, "intro", "description", "desc", "summary", "synopsis", "introduction"):
        info_for_intro["intro"] = intro

    cover_ext = (cover_ext or ".jpg").lower()
    if cover_ext == ".jpeg":
        cover_ext = ".jpg"
    if cover_ext not in (".jpg", ".png", ".webp", ".gif"):
        cover_ext = ".jpg"
    has_cover = bool(cover_bytes)
    cover_name = f"Images/cover{cover_ext}"
    cover_media = _cover_media_type(cover_ext)

    out_path = out_epub_path or (_safe_fs_name(book_title) + ".epub")
    out_parent = Path(out_path).parent
    if str(out_parent) not in ("", "."):
        out_parent.mkdir(parents=True, exist_ok=True)

    _safe_print(f"[Epub] Đang đóng gói EPUB: {out_path}")

    with zipfile.ZipFile(out_path, "w") as z:
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
        style_css = """
body { font-family: serif; line-height: 1.75; margin: 5%; }
h1 { font-size: 1.35em; line-height: 1.35; margin: 0 0 1em; text-align: center; }
p { margin: 0.65em 0; text-indent: 2em; }
.meta, .intro { text-indent: 0; }
.cover { text-align: center; margin: 0; text-indent: 0; }
.cover img { max-width: 100%; max-height: 95vh; height: auto; }
hr { border: 0; border-top: 1px solid #ddd; margin: 1em 0; }
""".strip()
        _epub_write(z, "OEBPS/Styles/style.css", style_css.encode("utf-8"))

        # 4) Text/*.xhtml + manifest/spine/navpoints
        manifest_items = [
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '<item id="style" href="Styles/style.css" media-type="text/css"/>',
            '<item id="titlepage" href="Text/title.xhtml" media-type="application/xhtml+xml"/>',
        ]
        spine_items = ['<itemref idref="titlepage"/>']
        navpoints = [
            '<navPoint id="nav-title" playOrder="1"><navLabel><text>Giới thiệu</text></navLabel>'
            '<content src="Text/title.xhtml"/></navPoint>'
        ]
        play_order = 2

        if has_cover:
            manifest_items.append(f'<item id="cover-image" href="{cover_name}" media-type="{cover_media}"/>')
            manifest_items.append('<item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.insert(0, '<itemref idref="cover"/>')
            navpoints.insert(
                0,
                '<navPoint id="nav-cover" playOrder="1"><navLabel><text>Cover</text></navLabel>'
                '<content src="Text/cover.xhtml"/></navPoint>',
            )
            navpoints[1] = navpoints[1].replace('playOrder="1"', 'playOrder="2"')
            play_order = 3

        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            xhtml = _xhtml_page(
                c["title"],
                f"<h1>{html.escape(c['title'])}</h1>\n"
                f"{_normalize_xhtml_fragment(c.get('content_html') or '<p>(Không có nội dung)</p>')}",
                language=language,
            ).encode("utf-8")
            _epub_write(z, "OEBPS/" + fn, xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            navpoints.append(
                f'<navPoint id="nav{i}" playOrder="{play_order}">'
                f'<navLabel><text>{html.escape(c["title"])}</text></navLabel>'
                f'<content src="{fn}"/></navPoint>'
            )
            play_order += 1

        _epub_write(z, "OEBPS/Text/title.xhtml", _xhtml_page(book_title, _intro_html(info_for_intro, tags=subject_source), language=language))

        if has_cover:
            _epub_write(z, "OEBPS/" + cover_name, cover_bytes)
            cover_body = f'<p class="cover"><img src="../{cover_name}" alt="{html.escape(book_title)}"/></p>'
            _epub_write(z, "OEBPS/Text/cover.xhtml", _xhtml_page("Cover", cover_body, language=language))

        # 6) content.opf
        uid = f"urn:uuid:{uuid4()}"
        now = datetime.now(timezone.utc)
        modified = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        subjects = subject_xml(subject_source)
        meta_cover = '<meta name="cover" content="cover-image"/>' if has_cover else ""
        guide_cover = '<guide><reference type="cover" title="Cover" href="Text/cover.xhtml"/></guide>' if has_cover else ""
        content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:identifier id="BookId">{html.escape(uid)}</dc:identifier>
    <dc:title>{html.escape(book_title)}</dc:title>
    <dc:creator opf:role="aut">{html.escape(author)}</dc:creator>
    <dc:publisher>{html.escape(publisher)}</dc:publisher>
{subjects}    <dc:language>{html.escape(language)}</dc:language>
    <dc:source>{html.escape(book_url or "")}</dc:source>
    <dc:date>{now.strftime("%Y-%m-%d")}</dc:date>
    <dc:contributor>{html.escape(creator)}</dc:contributor>
    <meta name="dcterms:modified" content="{modified}"/>
    {meta_cover}
  </metadata>
  <manifest>
    {"".join(manifest_items)}
  </manifest>
  <spine toc="ncx">
    {"".join(spine_items)}
  </spine>
  {guide_cover}
</package>
""".encode("utf-8")
        _epub_write(z, "OEBPS/content.opf", content_opf)

        # 7) toc.ncx
        toc = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{html.escape(uid)}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(book_title)}</text></docTitle>
  <navMap>{"".join(navpoints)}</navMap>
</ncx>
""".encode("utf-8")
        _epub_write(z, "OEBPS/toc.ncx", toc)

    _safe_print(f"[Epub] Đã tạo xong EPUB: {out_path}")
    return out_path
