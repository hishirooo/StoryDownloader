# -*- coding: utf-8 -*-
"""
Plugin cho domain: truyenfull.vision
- Lấy meta truyện (title/author/genres) và toàn bộ danh sách chương (mọi trang)
- Tải nội dung từng chương, lưu .html
- Tải ảnh bìa và đóng gói EPUB (metadata đầy đủ)
"""

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
from datetime import datetime, timezone
import requests, re, time, os, html, unicodedata, zipfile
from typing import Optional

# =============== CẤU HÌNH ===============
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}

# =============== TIỆN ÍCH ===============
def slugify_vi(s: str) -> str:
    """Bỏ dấu tiếng Việt -> ascii rồi slug."""
    s = (s or "truyen").strip()
    nfkd = unicodedata.normalize("NFKD", s)
    nd = "".join(c for c in nfkd if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9]+", "-", nd).strip("-").lower()
    s = re.sub(r"-{2,}", "-", s)
    return s or "truyen"

def normalize_chapter_title(t: Optional[str]) -> Optional[str]:
    """Sửa 'Chương10:...' -> 'Chương 10: ...'."""
    if not t: return t
    t = t.strip()
    m = re.match(r"^(Chương)\s*(\d+)(\s*[:\-–]?\s*)(.*)$", t, flags=re.I)
    if m:
        name, num, _, rest = m.groups()
        rest = rest.strip()
        return f"{name} {num}: {rest}" if rest else f"{name} {num}"
    return t

def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    for k in range(tries):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff*(k+1)); continue
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")

def _clean_to_list_url(url: str) -> str:
    p = urlparse(url)
    p = p._replace(query="", fragment="list-chapter")
    return urlunparse(p)

def _safe_filename(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:150] or "chapter"

# =============== DANH SÁCH CHƯƠNG ===============
def _get_total_pages(soup: BeautifulSoup) -> int:
    inp = soup.select_one('input#total-page[value]')
    if inp and inp["value"].isdigit():
        return int(inp["value"])
    last_a = soup.select_one('#list-chapter ul.pagination.pagination-sm a:-soup-contains("Cuối")')
    if last_a and last_a.get("href"):
        m = re.search(r"/trang-(\d+)/", last_a["href"])
        if m: return int(m.group(1))
    nums = []
    for a in soup.select('#list-chapter ul.pagination.pagination-sm a[href]'):
        m = re.search(r"/trang-(\d+)/", a["href"])
        if m: nums.append(int(m.group(1)))
    return max(nums) if nums else 1

def _build_page_url(page1_url: str, soup_page1: BeautifulSoup, page_idx: int) -> str:
    for a in soup_page1.select('#list-chapter ul.pagination.pagination-sm a[href]'):
        href = a["href"]
        if "/trang-" in href:
            replaced = re.sub(r"/trang-\d+/?", f"/trang-{page_idx}/", href)
            return replaced if replaced.startswith("http") else urljoin(page1_url, replaced)
    base = urlparse(page1_url)._replace(fragment="").geturl().rstrip("/")
    return f"{base}/trang-{page_idx}/#list-chapter"

def _extract_chapters_on_page(soup: BeautifulSoup, base_url: str):
    chs = []
    for a in soup.select('#list-chapter ul.list-chapter a[href]'):
        title = normalize_chapter_title(a.get_text(strip=True))
        href  = urljoin(base_url, a["href"])
        if title and href:
            chs.append({"title": title, "url": href})
    return chs

# =============== META TRUYỆN + COVER ===============
def _get_book_info(soup: BeautifulSoup):
    # Title
    title = None
    for sel in ["h1.title", "h3.title", ".title h1", ".title h3"]:
        n = soup.select_one(sel)
        if n:
            title = n.get_text(strip=True); break
    if not title:
        h = soup.find(["h1","h2","h3"])
        title = h.get_text(strip=True) if h else None

    # Author
    author = None
    n = soup.select_one('a[itemprop="author"]')
    if n:
        author = n.get_text(strip=True)
    else:
        h3 = soup.select_one('h3:-soup-contains("Tác giả")')
        if h3:
            a = h3.find_next("a")
            if a: author = a.get_text(strip=True)

    # Genres
    genres = [a.get_text(strip=True) for a in soup.select('a[itemprop="genre"]') if a.get_text(strip=True)]
    if not genres:
        h3 = soup.select_one('h3:-soup-contains("Thể loại")')
        if h3:
            container = h3.find_parent() or h3.parent
            if container:
                for a in container.find_all("a"):
                    t = a.get_text(strip=True)
                    if t: genres.append(t)
    seen=set(); genres=[g for g in genres if not (g in seen or seen.add(g))]
    return {"title": title, "author": author, "genres": genres}

def _fetch_cover_from_book_page(book_page_url: str):
    soup = _fetch_html(book_page_url)
    img = (soup.select_one(".book img[itemprop='image']") or
           soup.select_one(".books img[itemprop='image']") or
           soup.select_one(".book img") or
           soup.select_one(".books img") or
           soup.select_one("img[itemprop='image']"))
    if img and img.get("src"):
        src = urljoin(book_page_url, img["src"])
        r = requests.get(src, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        content = r.content
        ct = r.headers.get("Content-Type","").lower()
        ext = ".png" if ("png" in ct or src.lower().endswith(".png")) else ".jpg"
        return content, ext, src
    return None, None, None

# =============== API: LẤY DANH SÁCH CHƯƠNG ===============
def getText(url: str):
    list_url = _clean_to_list_url(url)
    soup1 = _fetch_html(list_url)
    info  = _get_book_info(soup1)
    total = _get_total_pages(soup1)

    out, seen = [], set()
    for c in _extract_chapters_on_page(soup1, list_url):
        if c["url"] not in seen:
            seen.add(c["url"]); out.append(c)
    for i in range(2, total + 1):
        page_url = _build_page_url(list_url, soup1, i)
        soup_i   = _fetch_html(page_url)
        for c in _extract_chapters_on_page(soup_i, page_url):
            if c["url"] not in seen:
                seen.add(c["url"]); out.append(c)
        time.sleep(SLEEP_BETWEEN_PAGES)

    return {
        "title": info["title"],
        "author": info["author"],
        "genres": info["genres"],
        "chapters": out,
        "total_pages": total,
    }

# =============== LẤY NỘI DUNG CHƯƠNG + HTML ===============
def _pick_chapter_title(soup: BeautifulSoup):
    for sel in [
        "a.chapter-title", "h1.title", "h2.title",
        ".chapter-title h1", ".chapter-title h2",
        ".chapter-title", "article h1", "article h2"
    ]:
        n = soup.select_one(sel)
        if n and n.get_text(strip=True):
            return normalize_chapter_title(n.get_text(strip=True))
    h = soup.find(["h1","h2","h3"])
    return normalize_chapter_title(h.get_text(strip=True)) if h else None

def _pick_chapter_content_node(soup: BeautifulSoup):
    for sel in [
        "#chapter-c", "#chapter-content", "div.chapter-content",
        "article .entry-content", ".entry-content",
        ".reading .content", ".box-chap", ".storytext", "#content", "div#content"
    ]:
        n = soup.select_one(sel)
        if n and n.get_text(strip=True):
            return n
    candidates = sorted(soup.find_all("div"), key=lambda d: len(d.get_text(" ", strip=True)), reverse=True)
    return candidates[0] if candidates else soup

def _clean_content(node: BeautifulSoup):
    remove_selectors = [
        "script","style","noscript","iframe","form",
        ".ads",".adsbygoogle",".ads-responsive",".ads-redirect-shopee",
        ".banner",".ad","[id^='ads-']",
        ".social",".social-share",".fb-comments",".comment-box",
        ".breadcrumb",".rate",".showmore",".author-note"
    ]
    for sel in remove_selectors:
        for t in node.select(sel):
            t.decompose()
    html_str = str(node)
    html_str = re.sub(r"<br\s*>", "<br/>", html_str, flags=re.I)
    html_str = re.sub(r"\s+\n", "\n", html_str)
    html_str = re.sub(r"<p>\s*(?:&nbsp;|\u00A0|\s)*</p>", "", html_str, flags=re.I)
    return html_str

def fetch_chapter_content(url: str):
    soup = _fetch_html(url)
    title = _pick_chapter_title(soup)
    node  = _pick_chapter_content_node(soup)
    content_html = _clean_content(node)
    return {"title": title, "content_html": content_html, "url": url}

HTML_TEMPLATE = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{doc_title}</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.7;max-width:820px;margin:2rem auto;padding:0 1rem;background:#f4f4f6;color:#222}}
    h1{{font-size:1.6rem;margin:0 0 1rem}}
    .meta{{color:#666;font-size:.9rem;margin-bottom:1rem}}
    img{{max-width:100%;height:auto}}
    p{{margin:.6rem 0}}
    article{{background:#fff;border-radius:12px;padding:1rem 1.2rem;box-shadow:0 1px 10px rgba(0,0,0,.06)}}
  </style>
</head>
<body>
  <h1>{chapter_title}</h1>
  <div class="meta">{book_title} · <a href="{src}">Nguồn</a></div>
  <article>
  {content}
  </article>
</body>
</html>"""

def save_chapter_html(book_title: str, chapter_idx: int, chap: dict, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{chapter_idx:04d} - {_safe_filename(chap.get('title') or f'Chuong {chapter_idx}')}.html"
    path  = os.path.join(out_dir, fname)
    html_out = HTML_TEMPLATE.format(
        doc_title     = f"{book_title} - {chap.get('title') or f'Chương {chapter_idx}'}",
        chapter_title = html.escape(chap.get('title') or f'Chương {chapter_idx}'),
        book_title    = html.escape(book_title or "Truyện"),
        src           = chap.get("url") or "",
        content       = chap.get("content_html") or "<p>(Không có nội dung)</p>",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_out)
    return path

def save_all_chapters_to_html(book_title: str, chapters: list, out_dir: str,
                              start: int = 1, end: Optional[int] = None):
    n = len(chapters)
    if end is None or end > n: end = n
    saved = []
    for i in range(start, end + 1):
        info = chapters[i-1]
        try:
            chap = fetch_chapter_content(info["url"])
            if not chap.get("title"): chap["title"] = info.get("title")
            p = save_chapter_html(book_title, i, chap, out_dir)
            print(f"[{i:04d}/{n}] Saved HTML: {p}", flush=True)
            saved.append(p)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"[{i:04d}/{n}] ERROR {info.get('url')}: {e}", flush=True)
    return saved

# =============== XUẤT EPUB ===============
def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)

def create_epub(book_url: str, book_title: str, author: str, chapters: list,
                out_epub_path: str, creator: str = "Hishiro", language: str = "vi"):
    """Tạo EPUB2 đơn giản có cover, TOC, metadata đầy đủ (không còn backslash trong f-string expressions)."""
    book_title = book_title or "Truyện"
    author = author or "—"

    # Thu thập nội dung chương
    items = []
    for idx, info in enumerate(chapters, 1):
        c = fetch_chapter_content(info["url"])
        if not c.get("title"):
            c["title"] = info.get("title") or f"Chương {idx}"
        c["title"] = normalize_chapter_title(c["title"])
        items.append(c)
        time.sleep(SLEEP_BETWEEN_CHAPS)

    # Cover
    cover_bytes, cover_ext, _ = _fetch_cover_from_book_page(_clean_to_list_url(book_url))
    cover_name = f"Images/cover{cover_ext}" if cover_bytes else None

    with zipfile.ZipFile(out_epub_path, "w") as z:
        # 1) mimetype
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        # 2) container.xml
        container_xml = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<container version=\"1.0\" xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">\n"
            "  <rootfiles>\n"
            "    <rootfile full-path=\"OEBPS/content.opf\" media-type=\"application/oebps-package+xml\"/>\n"
            "  </rootfiles>\n"
            "</container>"
        ).encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)

        # 3) Styles
        style_css = "body{font-family:serif;line-height:1.6} img{max-width:100%;height:auto} h1{font-size:1.4em;margin:0 0 .6em}"
        _epub_write(z, "OEBPS/Styles/style.css", style_css.encode("utf-8"))

        # 4) Text/*.xhtml + manifest/spine/navpoints
        manifest_items = []
        spine_items = []
        navpoints = []
        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            xhtml = (
                "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
                "<!DOCTYPE html>\n"
                f"<html xmlns=\"http://www.w3.org/1999/xhtml\" xml:lang=\"{language}\">\n"
                "<head>\n"
                f"  <title>{html.escape(c['title'])}</title>\n"
                "  <link href=\"../Styles/style.css\" rel=\"stylesheet\" type=\"text/css\"/>\n"
                "  <meta charset=\"utf-8\"/>\n"
                "</head>\n"
                "<body>\n"
                f"  <h1>{html.escape(c['title'])}</h1>\n"
                f"  <div>{c['content_html']}</div>\n"
                "</body>\n"
                "</html>"
            ).encode("utf-8")
            _epub_write(z, f"OEBPS/{fn}", xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            navpoints.append(
                f'<navPoint id="navPoint-{i}" playOrder="{i}">'
                f'<navLabel><text>{html.escape(c["title"])}</text></navLabel>'
                f'<content src="{fn}"/></navPoint>'
            )

        # 5) Cover
        if cover_name:
            _epub_write(z, f"OEBPS/{cover_name}", cover_bytes)

        # Chuẩn bị chuỗi (TRÁNH backslash trong f-string expressions)
        manifest_items_str = "\n    ".join(manifest_items)
        spine_items_str    = "\n    ".join(spine_items)
        navpoints_str      = "\n    ".join(navpoints)

        dt_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if cover_name and cover_name.lower().endswith(".png"):
            manifest_cover = f'<item id="cover-image" href="{cover_name}" media-type="image/png"/>'
        elif cover_name:
            manifest_cover = f'<item id="cover-image" href="{cover_name}" media-type="image/jpeg"/>'
        else:
            manifest_cover = ""
        guide_ref = f'<reference type="cover" title="Cover" href="{cover_name}"/>' if cover_name else ""

        # 6) content.opf
        content_opf = (
f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="BookID">urn:uuid:{slugify_vi(book_title)}-{int(datetime.now().timestamp())}</dc:identifier>
    <dc:title>{html.escape(book_title)}</dc:title>
    <dc:creator>{html.escape(author)}</dc:creator>
    <dc:language>{language}</dc:language>
    <meta name="cover" content="cover-image"/>
    <meta name="creator" content="{html.escape(creator)}"/>
    <meta property="dcterms:modified">{dt_utc}</meta>
  </metadata>
  <manifest>
    {manifest_cover}
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="Styles/style.css" media-type="text/css"/>
    {manifest_items_str}
  </manifest>
  <spine toc="ncx">
    {spine_items_str}
  </spine>
  <guide>
    {guide_ref}
  </guide>
</package>"""
        ).encode("utf-8")
        _epub_write(z, "OEBPS/content.opf", content_opf)

        # 7) toc.ncx
        toc_ncx = (
f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="urn:uuid:{slugify_vi(book_title)}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(book_title)}</text></docTitle>
  <navMap>
    {navpoints_str}
  </navMap>
</ncx>"""
        ).encode("utf-8")
        _epub_write(z, "OEBPS/toc.ncx", toc_ncx)

    return out_epub_path

