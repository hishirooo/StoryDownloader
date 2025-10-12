# -*- coding: utf-8 -*-
"""
Plugin cho domain: truyenfull.vision
- Lấy meta truyện (title/author/genres/status) và toàn bộ danh sách chương (mọi trang)
- Tải nội dung từng chương, lưu .html (chuẩn hóa layout)
- Cung cấp helper lấy cover cho EPUB
"""

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
import requests, re, os, html, unicodedata, time
from typing import Optional, List, Dict

# =============== CẤU HÌNH ===============
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 0.12
RETRY_STATUS = {429, 500, 502, 503, 504}

# =============== TIỆN ÍCH ===============
def _sess():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s

def _clean_txt(s: Optional[str]) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s, flags=re.S).strip()

def slugify_vi(s: str) -> str:
    s = (s or "truyen").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or "truyen"

def _fetch_html(url: str, tries: int = 3, backoff: float = 0.5) -> BeautifulSoup:
    ses = _sess()
    for k in range(tries):
        r = ses.get(url, timeout=TIMEOUT)
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff * (k + 1))
            continue
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")

def _clean_to_list_url(url: str) -> str:
    """Đưa URL về trang mục lục và gắn fragment #list-chapter để ổn định."""
    p = urlparse(url)
    p = p._replace(query="", fragment="list-chapter")
    return urlunparse(p)

def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:150] or "chapter"

# =============== CHUẨN HÓA TIÊU ĐỀ CHƯƠNG ===============
def normalize_chapter_title(t: Optional[str]) -> Optional[str]:
    if not t:
        return t
    t = t.strip()
    m = re.match(r"^(Chương)\s*(\d+)(\s*[:\-–]?\s*)(.*)$", t, flags=re.I)
    if m:
        name, num, _, rest = m.groups()
        rest = rest.strip()
        return f"{name} {num}: {rest}" if rest else f"{name} {num}"
    return t

# =============== DANH SÁCH CHƯƠNG ===============
def _get_total_pages(soup: BeautifulSoup) -> int:
    """
    Ưu tiên input#total-page; sau đó nút 'Cuối»'; sau đó số lớn nhất trong pagination.
    Nếu theme ẩn pagination, fallback quét toàn HTML. Trả 1 nếu không thấy gì.
    """
    # 1) hidden input (phổ biến)
    for sel in ['input#total-page[value]', 'input[id="total-page"][value]', 'input[name="total-page"][value]']:
        tag = soup.select_one(sel)
        if tag:
            val = str(tag.get("value", "")).strip()
            if val.isdigit():
                return int(val)

    # 2) link 'Cuối»'
    last_a = soup.select_one('#list-chapter ul.pagination.pagination-sm a:-soup-contains("Cuối")')
    if last_a and last_a.get("href"):
        m = re.search(r"/trang-(\d+)/", last_a["href"])
        if m:
            return int(m.group(1))

    # 3) số lớn nhất trong pagination
    nums: List[int] = []
    for a in soup.select('#list-chapter ul.pagination.pagination-sm a[href]'):
        m = re.search(r"/trang-(\d+)/", a.get("href", ""))
        if m:
            nums.append(int(m.group(1)))
    if nums:
        return max(nums)

    # 4) fallback: quét raw HTML
    raw = soup.decode() if hasattr(soup, "decode") else str(soup)
    nums = [int(x) for x in re.findall(r"/trang-(\d+)/", raw)]
    if nums:
        return max(nums)

    return 1

def _build_page_url(page1_url: str, soup_page1: BeautifulSoup, page_idx: int) -> str:
    """Dùng 1 anchor mẫu ở page1 để thay /trang-X/ -> /trang-i/; nếu không có, tự ghép."""
    for a in soup_page1.select('#list-chapter ul.pagination.pagination-sm a[href]'):
        href = a["href"]
        if "/trang-" in href:
            replaced = re.sub(r"/trang-\d+/?", f"/trang-{page_idx}/", href)
            return replaced if replaced.startswith("http") else urljoin(page1_url, replaced)
    base = urlparse(page1_url)._replace(fragment="").geturl().rstrip("/")
    return f"{base}/trang-{page_idx}/#list-chapter"

def _extract_chapters_on_page(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    chs: List[Dict[str, str]] = []
    for a in soup.select('#list-chapter ul.list-chapter a[href]'):
        title = normalize_chapter_title(a.get_text(strip=True))
        href  = urljoin(base_url, a["href"])
        if title and href:
            chs.append({"title": title, "url": href})
    return chs

# =============== META TRUYỆN + COVER ===============
def _get_book_info(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    # Title
    title = None
    for sel in ["h1.title", "h3.title", ".title h1", ".title h3"]:
        n = soup.select_one(sel)
        if n:
            title = _clean_txt(n.get_text()); break
    if not title:
        h = soup.find(["h1", "h2", "h3"])
        title = _clean_txt(h.get_text()) if h else None

    # Author
    author = None
    n = soup.select_one('a[itemprop="author"]')
    if n:
        author = _clean_txt(n.get_text())
    else:
        lab = soup.find(lambda t: t.name in ["h3", "p", "div"] and "Tác giả" in _clean_txt(t.get_text()))
        if lab:
            a = lab.find_next("a")
            if a:
                author = _clean_txt(a.get_text())

    # Genres
    genres = [ _clean_txt(a.get_text()) for a in soup.select('a[itemprop="genre"]') if _clean_txt(a.get_text()) ]
    if not genres:
        lab = soup.find(lambda t: t.name in ["h3","p","div"] and "Thể loại" in _clean_txt(t.get_text()))
        if lab:
            for a in (lab.find_all("a") or []):
                g = _clean_txt(a.get_text())
                if g: genres.append(g)
    seen=set(); genres=[g for g in genres if not (g in seen or seen.add(g))]

    # Status (tùy theme)
    status = None
    lab = soup.find(lambda t: t.name in ["h3","p","div"] and any(k in _clean_txt(t.get_text()) for k in ["Tình trạng","Trạng thái"]))
    if lab:
        ctx=_clean_txt(lab.get_text(" ", strip=True))
        m = re.search(r"(Đang ra|Hoàn thành|Tạm dừng)", ctx, flags=re.I)
        if m: status = m.group(1).title()

    return {"title": title, "author": author, "genres": genres, "status": status}

def _fetch_cover_from_book_page(book_page_url: str):
    """Trả về (bytes, ext, src) nếu có ảnh bìa."""
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
def getText(url: str) -> Dict:
    list_url = _clean_to_list_url(url)
    soup1 = _fetch_html(list_url)
    info  = _get_book_info(soup1)
    total = _get_total_pages(soup1)

    out, seen = [], set()

    # Trang 1
    for c in _extract_chapters_on_page(soup1, list_url):
        if c["url"] not in seen:
            seen.add(c["url"]); out.append(c)

    # Trang 2..N
    for i in range(2, total + 1):
        page_url = _build_page_url(list_url, soup1, i)
        soup_i   = _fetch_html(page_url)
        for c in _extract_chapters_on_page(soup_i, page_url):
            if c["url"] not in seen:
                seen.add(c["url"]); out.append(c)
        print(f"Loaded page {i}/{total} — total: {len(out)}")
        time.sleep(SLEEP_BETWEEN_PAGES)

    return {
        "title": info["title"],
        "author": info["author"],
        "genres": info["genres"],
        "status": info["status"] or "—",
        "chapters": out,
        "total_pages": total,
    }

# =============== LẤY NỘI DUNG CHƯƠNG + HTML ===============
def _pick_chapter_title(soup: BeautifulSoup) -> str:
    for sel in [
        "a.chapter-title", "h1.title", "h2.title",
        ".chapter-title h1", ".chapter-title h2",
        ".chapter-title", "article h1", "article h2"
    ]:
        n = soup.select_one(sel)
        if n and n.get_text(strip=True):
            return normalize_chapter_title(n.get_text(strip=True)) or "Chương ?"
    h = soup.find(["h1","h2","h3"])
    return normalize_chapter_title(h.get_text(strip=True)) if h else "Chương ?"

def _pick_chapter_content_node(soup: BeautifulSoup):
    for sel in [
        "#chapter-c", "#chapter-content", "div.chapter-content",
        "article .entry-content", ".entry-content",
        ".reading .content", ".box-chap", ".storytext", "#content", "div#content"
    ]:
        n = soup.select_one(sel)
        if n and n.get_text(strip=True):
            return n
    cands = sorted(soup.find_all("div"), key=lambda d: len(d.get_text(" ", strip=True)), reverse=True)
    return cands[0] if cands else soup

def _clean_content(node: BeautifulSoup) -> str:
    # bỏ rác
    for bad in node.select(
        "script,style,noscript,iframe,form,"
        ".ads,.adsbygoogle,.ads-responsive,.ads-redirect-shopee,"
        ".banner,.ad,[id^='ads-'],"
        ".social,.social-share,.fb-comments,.comment-box,"
        ".breadcrumb,.rate,.showmore,.author-note"
    ):
        bad.decompose()

    html_str = str(node)
    # dọn blank <p>, chuẩn hoá <br>
    html_str = re.sub(r"<p>\s*(?:&nbsp;|\u00A0|\s)*</p>", "", html_str, flags=re.I)
    html_str = re.sub(r"<br\s*>", "<br/>", html_str, flags=re.I)
    html_str = re.sub(r"\s+\n", "\n", html_str)
    return html_str

def fetch_chapter_content(url: str) -> Dict[str, str]:
    soup = _fetch_html(url)
    title = _pick_chapter_title(soup)
    node  = _pick_chapter_content_node(soup)
    content_html = _clean_content(node)
    return {"title": title, "content_html": content_html, "url": url}

# =============== LƯU HTML + INDEX ===============
HTML_TEMPLATE = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{doc_title}</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.7;max-width:860px;margin:2rem auto;padding:0 1rem;background:#f4f4f6;color:#222}}
    h1{{font-size:1.6rem;margin:0 0 1rem}}
    .meta{{color:#666;font-size:.9rem;margin-bottom:1rem}}
    img{{max-width:100%;height:auto}}
    p{{margin:.55rem 0}}
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

def save_chapter_html(book_title: str, chapter_idx: int, chap: Dict[str, str], out_dir: str) -> str:
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

def save_all_chapters_to_html(book_title: str, chapters: List[Dict[str,str]], out_dir: str,
                              start: int = 1, end: Optional[int] = None):
    n = len(chapters)
    if end is None or end > n: end = n
    for i in range(start, end + 1):
        info = chapters[i-1]
        try:
            chap = fetch_chapter_content(info["url"])
            if not chap.get("title"): chap["title"] = info.get("title")
            p = save_chapter_html(book_title, i, chap, out_dir)
            print(f"[{i:04d}/{n}] Saved HTML: {p}", flush=True)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"[{i:04d}/{n}] ERROR {info.get('url')}: {e}", flush=True)

def save_index_html(out_dir: str, title: str, author: str, genres: List[str], status: str,
                    chapters: List[Dict[str,str]]):
    os.makedirs(out_dir, exist_ok=True)
    items=[]
    for i, c in enumerate(chapters, 1):
        items.append(
            f'<li><a href="{i:04d} - {html.escape(c.get("title") or f"Chuong {i}")}.html">'
            f'{html.escape(c.get("title") or f"Chương {i}")}</a></li>'
        )
    genres_txt=", ".join(genres or [])
    html_doc=f"""<!doctype html>
<html lang="vi"><meta charset="utf-8"><title>{html.escape(title)} — Mục lục</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;line-height:1.7;
      padding:24px;max-width:860px;margin:0 auto;background:#f7f7f9;color:#222}}
h1{{font-size:1.8rem;margin:0 0 .6rem}}
.meta{{color:#555;margin:0 0 1rem}}
ol{{padding-left:1.25rem}}
.badge{{display:inline-block;background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;padding:.1rem .5rem;margin-right:.35rem}}
</style>
<h1>{html.escape(title)}</h1>
<div class="meta">
  <span class="badge">Tác giả: {html.escape(author or "—")}</span>
  <span class="badge">Thể loại: {html.escape(genres_txt or "—")}</span>
  <span class="badge">Tình trạng: {html.escape(status or "—")}</span>
</div>
<ol>
{''.join(items)}
</ol>
</html>"""
    with open(os.path.join(out_dir,"index.html"),"w",encoding="utf-8") as f:
        f.write(html_doc)

# (Giữ lại các helper để main/epub_builder có thể dùng)
__all__ = [
    "slugify_vi",
    "_clean_to_list_url",
    "_fetch_cover_from_book_page",
    "getText",
    "fetch_chapter_content",
    "save_all_chapters_to_html",
    "save_index_html",
]
