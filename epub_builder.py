# -*- coding: utf-8 -*-
"""
epub_builder: Module chung để đóng gói EPUB2 đơn giản.
PHIÊN BẢN TỐI ƯU: Không tải trùng, hỗ trợ cache từ HTML/dict
"""
from datetime import datetime, timezone
import zipfile, html, time, re, os, json, sys
from typing import Callable, List, Dict, Optional, Union
from pathlib import Path
from bs4 import BeautifulSoup

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
                sleep: float=0.12,
                out_epub_path: Optional[str]=None,
                html_cache_dir: Optional[str]=None,
                chapters_data: Optional[List[Dict]]=None):
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

    _safe_print(f"\n{'='*60}")
    _safe_print(f"BAT DAU TAO EPUB: {book_title}")
    _safe_print(f"{'='*60}")

    # Thu thập nội dung các chương
    items = []
    
    for idx, info in enumerate(chapters, 1):
        chapter_url = info["url"]
        chapter_title = info.get("title") or f"Chương {idx}"
        
        # ========== ƯU TIÊN 1: Dùng chapters_data nếu có ==========
        if chapters_data and idx <= len(chapters_data):
            c = chapters_data[idx - 1]
            _safe_print(f"[INFO] [{idx:04d}] Su dung du lieu da tai tu chapters_data: {c.get('title', chapter_title)}")
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

    _safe_print(f"\n{'='*60}")
    _safe_print(f"TONG KET:")
    _safe_print(f"   - Tong so chuong da xu ly: {len(items)}")
    _safe_print(f"   - Doc tu cache HTML: {used_cache_count}")
    _safe_print(f"   - Tai moi tu internet: {fetched_new_count}")
    _safe_print(f"{'='*60}\n")

    # Xử lý cover
    cover_name = None
    if cover_bytes:
        ext = (cover_ext or ".jpg").lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            ext = ".jpg"
        cover_name = "Images/cover" + ext

    # Đường dẫn EPUB đầu ra
    out_path = out_epub_path or (_safe_fs_name(book_title) + ".epub")

    _safe_print(f"[EPUB] Dang tao file EPUB: {out_path}")

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
                f"{_normalize_xhtml_fragment(c.get('content_html') or '<p>(Không có nội dung)</p>')}\n"
                "</body></html>"
            ).encode("utf-8")
            _epub_write(z, "OEBPS/" + fn, xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            navpoints.append(
                f'<navPoint id="nav{i}" playOrder="{i}">'
                f'<navLabel><text>{html.escape(c["title"])}</text></navLabel>'
                f'<content src="{fn}"/></navPoint>'
            )

        # 5) Cover (nếu có)
        manifest_cover = ""
        meta_cover = ""
        if cover_bytes and cover_name:
            _epub_write(z, "OEBPS/" + cover_name, cover_bytes)
            ctype = "image/png" if cover_name.lower().endswith(".png") else "image/jpeg"
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

    _safe_print(f"HOAN THANH! File EPUB: {out_path}\n")
    return out_path