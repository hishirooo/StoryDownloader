# -*- coding: utf-8 -*-
'''
    @Author: Hishio - Cá mụp cắn cáp
    @github: https://github.com/hishirooo
    @date: 7-12-2025
    @version: 1.0
    vozer.py - Tải truyện và tạo EPUB (vozer.io)
    Chế độ :
        1. Tải HTML
        2. Tải TXT
        3. Tải HTML + Tạo EPUB (đọc lại HTML có sẵn)
        4. Tải TXT  + Tạo EPUB (TXT -> bọc XHTML tối giản -> EPUB)
        5. Tải HTML + TXT + Tạo EPUB (EPUB dùng HTML có sẵn)
    Cover:
        - Sau khi nhập URL, nhập đường dẫn cover (file local hoặc URL ảnh).
        - Bỏ trống sẽ tự lấy cover từ DOM trang truyện.
        - Nếu ảnh khóa hình → convert 1 lần sang JPEG (cần Pillow).
    Log:
        - Thư mục đầu ra: ./output/<slug-title>
        - EPUB sẽ lưu ở: ./output/<slug-title>.epub (không nằm trong thư mục truyện)
        - Khi tải:   Saved : 0001 - TieuDeChuong.html / .txt
'''

from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, urlunparse, urljoin
import requests, ssl, urllib3, re, json, html, os, unicodedata, zipfile, io, datetime, shutil, time
from download_logger import chapter_log_line
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import importlib, re, sys, os, io, glob
from epub_metadata import PUBLISHER, subject_xml

# Thử import Pillow cho xử lý ảnh bìa
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("⚠ Cảnh báo: Không tìm thấy thư viện Pillow. Không thể tối ưu/chuyển đổi cover sang JPEG.")

# =============== CẤU HÌNH ===============
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}
EPUB_TARGET = "epub3"

# Kích thước tối đa cho ảnh bìa (Kobo/e-reader thân thiện)
MAX_COVER_SIZE = (1600, 2400) # (width, height)

def _text(el) -> str:
    '''
    Lấy text từ thẻ BeautifulSoup, trả về chuỗi rỗng nếu lỗi.
    '''
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""

def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    '''
    Tải HTML (có retry lỗi server).
    '''
    for k in range(tries):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)

        # Chỉ retry các lỗi server (5xx) và 429
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff * (k + 1))
            continue

        # Nếu là lỗi client (4xx) khác 429, dừng retry và raise
        if 400 <= r.status_code < 500:
            r.raise_for_status()

        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")

    # nếu ra khỏi vòng for mà chưa return (hiếm)
    raise RuntimeError(f"Không tải được URL: {url}")

#----------------------INFO TRUYỆN----------------------#
def _get_book_info(soup: BeautifulSoup) -> Dict[str, object]:
    """
    Lấy thông tin truyện vozer.io: Title / Author / Genres(list) / Status.
    Layout dựa theo block:
    <div class="p-2 leading-7 text-justify lg:flex-1"> ... </div>
    """
    info: Dict[str, object] = {
        "Title": "",
        "Author": "",
        "Genres": [],
        "Status": "",
        "CoverUrl": None,
    }

    # --- Title ---
    # Trên trang truyện vozer thường có <h1> Tên truyện </h1>
    title_tag = soup.find("h1") or soup.find("h2")
    title = _text(title_tag)
    info["Title"] = title or "Truyện"

    # --- Block thông tin chính ---
    node_info = soup.find("div", class_="p-2 leading-7 text-justify lg:flex-1")

    def find_p_contains(node, pattern: str):
        """
        Tìm thẻ <p> sao cho get_text() match regex pattern.
        """
        if not node:
            return None
        return node.find(
            lambda tag: tag.name == "p" and re.search(pattern, tag.get_text(), re.IGNORECASE)
        )

    # Author
    author_tag = find_p_contains(node_info, r"Tác giả")
    if author_tag:
        info["Author"] = _text(author_tag).replace("Tác giả:", "").strip()
    else:
        info["Author"] = "—"

    # Genres (trên vozer chỉ có 1 thể loại chính, nhưng mình để dạng list cho đồng bộ)
    genres: List[str] = []
    genre_tag = find_p_contains(node_info, r"Thể loại")
    if genre_tag:
        # có thể chứa <a> Tiên Hiệp </a>
        for a in genre_tag.find_all("a"):
            t = _text(a)
            if t:
                genres.append(t)
        if not genres:
            txt = _text(genre_tag).replace("Thể loại:", "").strip()
            if txt:
                genres.append(txt)
    info["Genres"] = genres

    # Status
    status_tag = find_p_contains(node_info, r"Trạng thái")
    if status_tag:
        info["Status"] = _text(status_tag).replace("Trạng thái:", "").strip()
    else:
        info["Status"] = "N/A"

    # Cover URL (chỉ để hiển thị / debug, build EPUB dùng _fetch_cover_from_book_page)
    cover_div = soup.find("div", class_="lg:w-1/4")
    cover_img = cover_div.find("img") if cover_div else None
    if cover_img and cover_img.has_attr("src"):
        info["CoverUrl"] = urljoin("https://vozer.io", cover_img["src"])
    else:
        info["CoverUrl"] = None

    return info

#----------------------COVER / ẢNH BÌA----------------------#
def _fetch_cover_from_book_page(book_page_url: str):
    """
    Lấy ảnh bìa từ trang truyện vozer.
    Trả về (bytes, ext, url) hoặc (None, None, None).
    """
    soup = _fetch_html(book_page_url)
    # layout: <div class="lg:w-1/4"> <img src="..."/> ...
    img = None

    cover_div = soup.find("div", class_="lg:w-1/4")
    if cover_div:
        img = cover_div.find("img")

    if not img:
        # fallback: lấy img đầu tiên trong trang
        img = soup.find("img")

    if img and img.get("src"):
        src = urljoin(book_page_url, img["src"])
        r = requests.get(src, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        content = r.content
        ct = r.headers.get("Content-Type", "").lower()
        ext = ".png" if ("png" in ct or src.lower().endswith(".png")) else ".jpg"
        return content, ext, src
    return None, None, None

def _sniff_image_type(data: bytes) -> tuple[str, str]:
    """
    Đoán định dạng ảnh từ header.
    Trả về (ext, mime). Nếu không biết: ('.bin', 'application/octet-stream')
    """
    if not data or len(data) < 12:
        return (".bin", "application/octet-stream")
    if data[:3] == b"\xff\xd8\xff":
        return (".jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return (".png", "image/png")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return (".webp", "image/webp")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return (".gif", "image/gif")
    return (".bin", "application/octet-stream")

def _ensure_jpeg_cover(img_bytes: bytes) -> tuple[bytes, str, str]:
    """
    Chuyển ảnh bất kỳ về JPEG (RGB, quality=90).
    Có resize cho thân thiện e-reader.
    """
    if not HAS_PILLOW:
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)

    try:
        import io as _io
        im = Image.open(_io.BytesIO(img_bytes))

        # Resize nếu vượt quá giới hạn
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
            print(f"✔ Cover resized to: {im.size[0]}x{im.size[1]} (max: {MAX_COVER_SIZE[0]}x{MAX_COVER_SIZE[1]})")

        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")

        out = _io.BytesIO()
        im.save(out, format="JPEG", quality=90, optimize=True)
        return (out.getvalue(), ".jpg", "image/jpeg")

    except Exception as e:
        ext, mime = _sniff_image_type(img_bytes)
        print(f"⚠ Không convert/resize được cover sang JPEG: {e}. Dùng ảnh gốc {ext}.")
        return (img_bytes, ext, mime)

def _clean_to_list_url(url: str) -> str:
    """
    Với vozer, URL truyện & URL list chương cùng 1 trang, phân trang bằng ?pagechap.
    Ta chỉ cần bỏ query/fragment.
    """
    p = urlparse(url)
    p = p._replace(query="", fragment="")
    return urlunparse(p)

def _load_cover_from_input(cover_in: str, story_url: str | None) -> tuple[bytes | None, str | None, str | None]:
    """
    Nếu cover_in:
      - URL → tải bằng requests
      - Local path → đọc file
    Nếu cover_in rỗng:
      - nếu có story_url → lấy từ trang truyện vozer
    Sau đó luôn cố convert JPEG/resize.
    """
    data = None
    if cover_in:
        try:
            if cover_in.lower().startswith(("http://", "https://")):
                print(f"Tải cover từ URL: {cover_in}")
                r = requests.get(cover_in, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.content
            else:
                print(f"Đọc cover từ file: {cover_in}")
                with open(cover_in, "rb") as f:
                    data = f.read()
        except Exception as e:
            print(f"⚠ Không tải/đọc được cover từ '{cover_in}': {e}")

    if data is None and story_url:
        try:
            print("Tự động lấy cover từ trang truyện (vozer)...")
            cb, ext, _ = _fetch_cover_from_book_page(_clean_to_list_url(story_url))
            if cb:
                data = cb
        except Exception as e:
            print(f"⚠ Không lấy được cover từ web: {e}")

    if data:
        return _ensure_jpeg_cover(data)

    return (None, None, None)

#----------------------CHAPTER LIST----------------------#
def _check_navigation(soup: BeautifulSoup) -> int:
    """
    Trả về số trang mục lục (>=1) dựa vào <nav role="navigation"> của vozer.
    Không có phân trang => trả về 1.
    """
    nav = soup.find("nav", role="navigation")
    if not nav:
        return 1

    links = nav.find_all("a")
    if len(links) < 2:
        return 1

    # Thường link áp chót là số trang cuối
    last_page_link = links[-2]
    text = last_page_link.get_text(strip=True)
    try:
        last_page_num = int(text)
    except:
        last_page_num = 1

    return max(last_page_num, 1)

def _gen_url_page(urlStory: str, lastpage: int) -> list[str]:
    """
    Tạo danh sách URL pagechap cho các trang 2..lastpage.
    """
    pages = []
    for page in range(2, lastpage + 1):
        pages.append(f"{urlStory}?pagechap={page}")
    return pages

def _extract_chapters_on_page(soup: BeautifulSoup, base_url: str) -> list[Dict[str, str]]:
    """
    Lấy danh sách chương trên 1 trang vozer.
    Trả về list[dict] với keys: title, url
    """
    chs: List[Dict[str, str]] = []
    div_chapter = soup.find("div", class_="p-3 leading-8 shadow bg-gray-002")
    if not div_chapter:
        return chs

    # Thường có 2 bảng: 5 chương mới + full list -> dùng tbody cuối
    tbodies = div_chapter.find_all("tbody")
    if not tbodies:
        return chs
    tbody = tbodies[-1]

    for tr in tbody.find_all("tr"):
        link = tr.find("a", href=True)
        if not link:
            continue
        title = _text(link)
        href = urljoin(base_url, link["href"])
        if title and href:
            chs.append({"title": title, "url": href})
    return chs

def _get_list_chapters(book_page_url: str) -> List[Dict[str, str]]:
    """
    Lấy list chương vozer: trả về List[Dict] với keys: title, url
    (tương tự _get_list_chapters bên truyenfull_vision nhưng dùng layout vozer).
    """
    list_url = _clean_to_list_url(book_page_url)
    soup_page1 = _fetch_html(list_url)
    total_pages = _check_navigation(soup_page1)
    all_chapters: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    # Trang 1
    for c in _extract_chapters_on_page(soup_page1, list_url):
        if c["url"] not in seen_urls:
            seen_urls.add(c["url"])
            all_chapters.append(c)

    # Các trang còn lại
    if total_pages > 1:
        for i in range(2, total_pages + 1):
            page_url = f"{list_url}?pagechap={i}"
            print(f"Fetching chapter list page {i}/{total_pages}... {page_url}", flush=True)
            soup_i = _fetch_html(page_url)
            for c in _extract_chapters_on_page(soup_i, page_url):
                if c["url"] not in seen_urls:
                    seen_urls.add(c["url"])
                    all_chapters.append(c)
            if i < total_pages:
                time.sleep(SLEEP_BETWEEN_PAGES)

    return all_chapters

#----------------------GET CONTENT CHAPTER----------------------#
def _safe_filename(s: str) -> str:
    '''
    Tạo tên file an toàn từ chuỗi đầu vào.
    Loại bỏ các ký tự không hợp lệ và giới hạn độ dài.
    '''
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:150] or "chapter"

def normalize_chapter_title(t: Optional[str]) -> Optional[str]:
    """Sửa 'Chương10:...' -> 'Chương 10: ...' (dùng chung)."""
    if not t:
        return t
    t = t.strip()
    m = re.match(r"^(Chương)\s*(\d+)(\s*[:\-–]?\s*)(.*)$", t, flags=re.I)
    if m:
        name, num, _, rest = m.groups()
        rest = rest.strip()
        return f"{name} {num}: {rest}" if rest else f"{name} {num}"
    return t

def _pick_chapter_title(soup: BeautifulSoup):
    '''
    Lấy tiêu đề chương từ soup.
    Dùng generic selector + fallback h1/h2/h3, dùng chung cho vozer.
    '''
    for sel in [
        "a.chapter-title", "h1.title", "h2.title",
        ".chapter-title h1", ".chapter-title h2",
        ".chapter-title", "article h1", "article h2"
    ]:
        n = soup.select_one(sel)
        if n and n.get_text(strip=True):
            return normalize_chapter_title(n.get_text(strip=True))
    h = soup.find(["h1", "h2", "h3"])
    return normalize_chapter_title(h.get_text(strip=True)) if h else None

def _pick_chapter_content_node(soup: BeautifulSoup):
    '''
    Lấy nội dung chương từ soup.
    Với vozer không có id cố định -> nếu selector không trúng, chọn div dài nhất.
    '''
    # Nếu vozer có class riêng, có thể thêm sau này, ví dụ:
    # "div.leading-7", ".reading-content", ...
    for sel in [
        "#chapter-c", "#chapter-content", "div.chapter-content",
        "article .entry-content", ".entry-content",
        ".reading .content", ".box-chap", ".storytext", "#content", "div#content"
    ]:
        n = soup.select_one(sel)
        if n and n.get_text(strip=True):
            return n

    # Fallback: chọn div có text dài nhất
    candidates = sorted(
        soup.find_all("div"),
        key=lambda d: len(d.get_text(" ", strip=True)),
        reverse=True
    )
    return candidates[0] if candidates else soup

def _clean_content(node: BeautifulSoup):
    '''
    Làm sạch nội dung chương: bỏ ads, script,...
    '''
    remove_selectors = [
        "script", "style", "noscript", "iframe", "form",
        ".ads", ".adsbygoogle", ".ads-responsive", ".ads-redirect-shopee",
        ".banner", ".ad", "[id^='ads-']",
        ".social", ".social-share", ".fb-comments", ".comment-box",
        ".breadcrumb", ".rate", ".showmore", ".author-note"
    ]
    for sel in remove_selectors:
        for t in node.select(sel):
            t.decompose()

    html_str = str(node)

    # ⚡ CẮT PHẦN "Đề xuất Voz: ..."
    # Xóa mọi thứ từ "Đề xuất Voz:" tới hết node
    html_str = re.sub(r"Đề\s*xuất\s*Voz:.*$", "", html_str, flags=re.I | re.S)

    # Các bước cleanup cũ
    html_str = re.sub(r"<br\s*>", "<br/>", html_str, flags=re.I)
    html_str = re.sub(r"\s+\n", "\n", html_str)
    html_str = re.sub(r"<p>\s*(?:&nbsp;|\u00A0|\s)*</p>", "", html_str, flags=re.I)
    return html_str

def fetch_chapter_content(url: str):
    '''Lấy nội dung chương từ URL vozer (HTML).'''
    soup = _fetch_html(url)
    title = _pick_chapter_title(soup)
    node = _pick_chapter_content_node(soup)
    content_html = _clean_content(node)
    return {"title": title, "content_html": content_html, "url": url}

#----------------------SAVE CONTENT CHAPTER----------------------#
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
    '''Lưu chương dưới dạng file HTML. Trả về đường dẫn file đã lưu.'''
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
                              start: int = 1, end: Optional[int] = None) -> list[str]:
    '''
    Lưu tất cả chương trong danh sách chapters (vozer) thành file HTML.
    chapters: list[dict{title,url}]
    '''
    n = len(chapters)
    if end is None or end > n:
        end = n
    saved: List[str] = []

    selected_total = end - start + 1
    for done, i in enumerate(range(start, end + 1), 1):
        info = chapters[i - 1]
        try:
            chap = fetch_chapter_content(info["url"])
            if not chap.get("title"):
                chap["title"] = info.get("title")
            p = save_chapter_html(book_title, i, chap, out_dir)
            print(chapter_log_line(done, selected_total, chap.get("status_code", 200), i, n, chap.get("title") or info.get("title") or ""), flush=True)
            saved.append(p)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(chapter_log_line(done, selected_total, "ERR", i, n, f"{info.get('title') or info.get('url')} ({e})"), flush=True)
    return saved

def _html_article_text(soup: BeautifulSoup) -> str:
    """
    Lấy text 'đẹp' từ HTML chương:
    - Ưu tiên <article> nếu có
    - Chuyển <br> -> \n, gom đoạn.
    """
    for br in soup.find_all("br"):
        br.replace_with("\n")
    node = soup.select_one("article") or soup.body or soup
    txt = node.get_text("\n", strip=True)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt

def _txt_to_html_content(text: str) -> str:
    """
    Chuyển TXT thuần sang HTML content:
    - Tách đoạn theo dòng trống
    - Escape HTML, giữ xuống dòng bằng <br/>
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    html_blocks = []
    for b in blocks:
        safe = html.escape(b).replace("\n", "<br/>")
        html_blocks.append(f"<p>{safe}</p>")
    return "\n".join(html_blocks) if html_blocks else "<p>(Trống)</p>"

def convert_htmls_to_txts(out_dir: str, only_paths: list[str] | None = None) -> list[str]:
    """
    Quét *.html trong out_dir, bóc nội dung và lưu *.txt cạnh đó.
    """
    html_files = sorted(only_paths) if only_paths else sorted(glob.glob(os.path.join(out_dir, "*.html")))
    saved_txts: List[str] = []
    for path in html_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), "html.parser")
            txt = _html_article_text(soup)
            base = os.path.splitext(os.path.basename(path))[0]
            out_txt = os.path.join(out_dir, f"{base}.txt")
            with open(out_txt, "w", encoding="utf-8") as wf:
                wf.write(txt)
            print(f"→ TXT: {out_txt}")
            saved_txts.append(out_txt)
        except Exception as e:
            print(f"ERR convert_htmls_to_txts({path}): {e}")
    return saved_txts

def convert_txts_to_htmls(out_dir: str, book_title: str | None = None) -> list[str]:
    """
    Quét *.txt trong out_dir và wrap lại theo HTML_TEMPLATE.
    Tự đoán tiêu đề chương từ tên file hoặc dòng đầu tiên.
    """
    saved: List[str] = []
    if not book_title:
        book_title = os.path.basename(os.path.normpath(out_dir)) or "Truyện"

    txt_files = sorted(glob.glob(os.path.join(out_dir, "*.txt")))
    for idx, path in enumerate(txt_files, 1):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            base = os.path.splitext(os.path.basename(path))[0]
            m = re.match(r"^\s*(\d+)\s*-\s*(.+)$", base)
            if m:
                chapter_idx = int(m.group(1))
                title_guess = m.group(2).strip()
            else:
                chapter_idx = idx
                title_guess = next((ln.strip() for ln in raw.splitlines() if ln.strip()), f"Chương {chapter_idx}")
            title_guess = normalize_chapter_title(title_guess)

            content_html = _txt_to_html_content(raw)
            chap = {"title": title_guess, "content_html": content_html, "url": ""}

            out_html = save_chapter_html(book_title, chapter_idx, chap, out_dir)
            saved.append(out_html)
            print(f"→ HTML: {out_html}")
        except Exception as e:
            print(f"ERR convert_txts_to_htmls({path}): {e}")
    return sorted(saved)

def save_all_chapters_to_txt(book_title: str, chapters: list, out_dir: str,
                             start: int = 1, end: int | None = None) -> list[str]:
    """
    Lưu tất cả chương thành .txt (fetch trực tiếp, không cần HTML trung gian).
    Pattern tên file: '0001 - <title>.txt'
    """
    n = len(chapters)
    if end is None or end > n:
        end = n
    os.makedirs(out_dir, exist_ok=True)
    saved: List[str] = []

    selected_total = end - start + 1
    for done, i in enumerate(range(start, end + 1), 1):
        info = chapters[i - 1]
        try:
            c = fetch_chapter_content(info["url"])
            if not c.get("title"):
                c["title"] = info.get("title") or f"Chương {i}"
            soup = BeautifulSoup(c.get("content_html") or "", "html.parser")
            txt = _html_article_text(soup)
            fname = f"{i:04d} - {_safe_filename(c['title'])}.txt"
            path = os.path.join(out_dir, fname)
            with open(path, "w", encoding="utf-8") as f:
                f.write(txt)
            print(chapter_log_line(done, selected_total, c.get("status_code", 200), i, n, c.get("title") or info.get("title") or ""), flush=True)
            saved.append(path)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(chapter_log_line(done, selected_total, "ERR", i, n, f"{info.get('title') or info.get('url')} ({e})"), flush=True)
    return saved

#-----------------------CREATE EPUB-----------------------#
def _slugify_vi(s: str) -> str:
    """Slug ASCII an toàn cho tên file (Windows/Kobo thân thiện)."""
    s = (s or "book").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-_")
    return (s or "book")[:80]

def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)

def create_epub(book_url: str | None,
                book_title: str,
                author: str,
                chapters: list,            # list[str path_html] HOẶC list[dict{url,title?}]
                out_epub_path: str,
                creator: str = "Hishiro",
                language: str = "vi",
                epub_target: str | None = None,
                cover_bytes: bytes | None = None,
                cover_ext: str | None = None,
                cover_mime: str | None = None,
                tags=None) -> str:
    """
    Tạo EPUB2/EPUB3, reuse logic từ truyenfull_vision (chapters có thể là path HTML hoặc dict URL).
    """
    book_title = book_title or "Truyện"
    author     = author or "—"
    target     = (epub_target or EPUB_TARGET).lower().strip()

    out_is_dir = (os.path.isdir(out_epub_path) or not out_epub_path.lower().endswith(".epub"))
    if out_is_dir:
        os.makedirs(out_epub_path, exist_ok=True)
        out_file = os.path.join(out_epub_path, f"{_slugify_vi(book_title)}.epub")
    else:
        os.makedirs(os.path.dirname(out_epub_path) or ".", exist_ok=True)
        out_file = out_epub_path

    # Thu thập nội dung chương
    items: List[Dict[str, str]] = []
    if chapters and isinstance(chapters[0], str):
        # Đọc HTML cục bộ
        for i, html_path in enumerate(chapters, 1):
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    soup = BeautifulSoup(f.read(), "html.parser")
                title_node = soup.find("h1") or soup.find("title")
                if title_node:
                    title = normalize_chapter_title(title_node.get_text(strip=True))
                else:
                    base = os.path.splitext(os.path.basename(html_path))[0]
                    m = re.match(r"^\s*\d+\s*-\s*(.+)$", base)
                    title = normalize_chapter_title(m.group(1).strip()) if m else base
                node = soup.select_one("article") or soup.body or soup
                content_html = "".join(str(x) for x in node.children)
                items.append({"title": title, "content_html": content_html})
            except Exception as e:
                print(f"WARN đọc HTML '{html_path}': {e}")
    else:
        # Fetch trực tiếp (ít dùng cho vozer vì đã có save_all_chapters_to_html)
        for idx, info in enumerate(chapters or [], 1):
            c = fetch_chapter_content(info["url"])
            if not c.get("title"):
                c["title"] = info.get("title") or f"Chương {idx}"
            c["title"] = normalize_chapter_title(c["title"])
            items.append(c)
            time.sleep(SLEEP_BETWEEN_CHAPS)

    # Cover fallback nếu chưa có
    if cover_bytes is None and book_url:
        try:
            print("Fallback: Tự động lấy cover từ trang truyện (create_epub)...")
            cb, ext, _ = _fetch_cover_from_book_page(_clean_to_list_url(book_url))
            if cb:
                cover_bytes, cover_ext, cover_mime = _ensure_jpeg_cover(cb)
        except Exception:
            cover_bytes, cover_ext, cover_mime = None, None, None

    if cover_bytes is not None and (not cover_ext or not cover_mime):
        cover_ext, cover_mime = _sniff_image_type(cover_bytes)

    cover_relpath = f"Images/cover{cover_ext}" if (cover_bytes and cover_ext) else None

    with zipfile.ZipFile(out_file, "w") as z:
        # mimetype
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        # container.xml
        container_xml = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<container version=\"1.0\" xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">\n"
            "  <rootfiles>\n"
            "    <rootfile full-path=\"OEBPS/content.opf\" media-type=\"application/oebps-package+xml\"/>\n"
            "  </rootfiles>\n"
            "</container>"
        ).encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)

        # cover.xhtml (page)
        if cover_relpath:
            cover_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi">\n'
                '<head>\n'
                '  <title>Cover</title>\n'
                '  <meta charset="utf-8"/>\n'
                '  <style type="text/css">.cover{height:100vh;max-width:100%;object-fit:contain;margin:0 auto;display:block}</style>\n'
                '</head>\n'
                '<body>\n'
                f'  <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="100%" height="100%" viewBox="0 0 1000 1500" preserveAspectRatio="xMidYMid meet">\n'
                f'    <image width="1000" height="1500" xlink:href="../{cover_relpath}" class="cover"/>\n'
                f'  </svg>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/Text/cover.xhtml", cover_xhtml)

        # Styles
        style_css = (
            "body{font-family:serif;line-height:1.6}"
            "img{max-width:100%;height:auto}"
            "h1{font-size:1.4em;margin:0 0 .6em}"
        )
        _epub_write(z, "OEBPS/Styles/style.css", style_css.encode("utf-8"))

        # Chapters
        manifest_items, spine_items, navpoints = [], [], []
        chap_refs: List[Tuple[str, str]] = []

        if cover_relpath:
            manifest_items.append('<item id="cover-html" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="cover-html" linear="no"/>')

        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            chapter_xhtml = (
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
            _epub_write(z, f"OEBPS/{fn}", chapter_xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            navpoints.append(
                f'<navPoint id="navPoint-{i}" playOrder="{i}" class="chapter">'
                f'<navLabel><text>{html.escape(c["title"])}</text></navLabel>'
                f'<content src="{fn}"/></navPoint>'
            )
            chap_refs.append((fn, c["title"]))

        if cover_relpath and cover_bytes:
            _epub_write(z, f"OEBPS/{cover_relpath}", cover_bytes)

        manifest_items_str = "\n    ".join(manifest_items)
        spine_items_str    = "\n    ".join(spine_items)
        navpoints_str      = "\n    ".join(navpoints)
        dt_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        subjects = subject_xml(tags, indent="    ")
        cover_media_item = f'<item id="cover-image" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}"/>' if cover_relpath else ""

        if target == "epub3":
            if cover_relpath:
                cover_item = f'<item id="cover-img" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}" properties="cover-image"/>'
            else:
                cover_item = ""

            # nav.xhtml
            nav_html = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" xmlns:epub="http://www.idpf.org/2007/ops">\n'
                '<head>\n'
                '  <meta charset="utf-8"/>\n'
                '  <title>Table of Contents</title>\n'
                '  <link href="Styles/style.css" rel="stylesheet" type="text/css"/>\n'
                '</head>\n'
                '<body>\n'
                '  <nav epub:type="toc" id="toc">\n'
                f'    <h1>{html.escape(book_title)}</h1>\n'
                '    <ol>\n' +
                "\n".join(f'      <li><a href="{fn}">{html.escape(t)}</a></li>' for fn, t in chap_refs) +
                '\n    </ol>\n'
                '  </nav>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/nav.xhtml", nav_html)

            content_opf = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="3.0">\n'
                '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
                f'    <dc:identifier id="BookID">urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}</dc:identifier>\n'
                f'    <dc:title>{html.escape(book_title)}</dc:title>\n'
                f'    <dc:creator>{html.escape(author)}</dc:creator>\n'
                f'    <dc:language>{language}</dc:language>\n'
                f'    <meta name="creator" content="{html.escape(creator)}"/>\n'
                f'    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>\n'
                f'{subjects}'
                f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
                '  </metadata>\n'
                '  <manifest>\n'
                f'    {cover_media_item}\n'
                '    <item id="css" href="Styles/style.css" media-type="text/css"/>\n'
                '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
                f'    {manifest_items_str}\n'
                '  </manifest>\n'
                '  <spine>\n'
                f'    {spine_items_str}\n'
                '  </spine>\n'
                '</package>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/content.opf", content_opf)
        else:
            if cover_relpath:
                manifest_cover = f'<item id="cover-image" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}"/>'
                guide_ref = f'<reference type="cover" title="Cover" href="Text/cover.xhtml"/>'
            else:
                manifest_cover = ""
                guide_ref = ""

            content_opf = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="2.0">\n'
                '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
                f'    <dc:identifier id="BookID">urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}</dc:identifier>\n'
                f'    <dc:title>{html.escape(book_title)}</dc:title>\n'
                f'    <dc:creator>{html.escape(author)}</dc:creator>\n'
                f'    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>\n'
                f'{subjects}'
                f'    <dc:language>{language}</dc:language>\n'
                '    <meta name="cover" content="cover-image"/>\n'
                f'    <meta name="creator" content="{html.escape(creator)}"/>\n'
                f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
                '  </metadata>\n'
                '  <manifest>\n'
                f'    {cover_media_item}\n'
                f'    {manifest_cover}\n'
                '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
                '    <item id="css" href="Styles/style.css" media-type="text/css"/>\n'
                f'    {manifest_items_str}\n'
                '  </manifest>\n'
                '  <spine toc="ncx">\n'
                f'    {spine_items_str}\n'
                '  </spine>\n'
                '  <guide>\n'
                f'    {guide_ref}\n'
                '  </guide>\n'
                '</package>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/content.opf", content_opf)

            toc_ncx = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"\n'
                '  "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
                '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
                '  <head>\n'
                f'    <meta name="dtb:uid" content="urn:uuid:{_slugify_vi(book_title)}"/>\n'
                '    <meta name="dtb:depth" content="1"/>\n'
                '    <meta name="dtb:totalPageCount" content="0"/>\n'
                '    <meta name="dtb:maxPageNumber" content="0"/>\n'
                '  </head>\n'
                f'  <docTitle><text>{html.escape(book_title)}</text></docTitle>\n'
                '  <navMap>\n'
                f'    {navpoints_str}\n'
                '  </navMap>\n'
                '</ncx>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/toc.ncx", toc_ncx)

    return out_file

def build_epub(epub_out_dir: str, title: str, author: str, html_paths: list[str],
               cover_bytes: bytes | None = None, cover_ext: str | None = None, cover_mime: str | None = None,
               epub_target: str | None = None, tags=None) -> str:
    """
    Nhận list đường dẫn HTML, sắp xếp, rồi gọi create_epub.
    """
    paths = [p for p in (html_paths or []) if isinstance(p, str) and p.lower().endswith(".html") and os.path.isfile(p)]
    if not paths:
        raise ValueError("build_epub: Không có file HTML hợp lệ để đóng EPUB.")

    def _sort_key(p: str):
        base = os.path.splitext(os.path.basename(p))[0]
        m = re.match(r"^\s*(\d+)", base)
        idx = int(m.group(1)) if m else 10**9
        return (idx, base.casefold())
    paths.sort(key=_sort_key)

    return create_epub(
        book_url=None,
        book_title=title,
        author=author,
        chapters=paths,
        out_epub_path=epub_out_dir,
        creator="Hishiro",
        language="vi",
        epub_target=epub_target or EPUB_TARGET,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime,
        tags=tags,
    )

def _delete_files(paths: list[str]):
    '''
    Xóa file trong list paths.
    '''
    ok = 0
    for p in paths or []:
        try:
            if os.path.isfile(p):
                os.remove(p)
                ok += 1
        except Exception as e:
            print(f"⚠ Không xóa được {p}: {e}")
    print(f"🧹 Đã xóa {ok}/{len(paths or [])} file")

#-----------------------CLI CHÍNH-----------------------#
def main():
    StoryUrl = input("Nhập URL truyện vozer.io: ").strip()
    cover_in = input("Nhập đường dẫn Cover (bỏ trống để tự lấy từ web): ").strip()

    print("--- 1. Fetching Book Info ---")
    info = _get_book_info(_fetch_html(StoryUrl))
    title  = info.get("Title") or "Truyện"
    author = info.get("Author") or "—"

    print("--- 2. Fetching Chapters List ---")
    chapters = _get_list_chapters(StoryUrl)

    print("----------------STORY INFO----------------")
    print("Title :", title)
    print("Author:", author)
    print("Genre :", ", ".join(info.get("Genres", [])) or "—")
    print("Status:", info.get("Status", "—"))
    print("Total Chapters:", len(chapters))
    print("------------------------------------------")

    print("\nChọn chức năng:")
    print("[1]: Tải và lưu dạng HTML")
    print("[2]: Tải và lưu dạng TXT (trực tiếp)")
    print("[3]: Tải và lưu dạng HTML + TXT")
    print("[4]: Tải và lưu dạng HTML + Build EPUB")
    print("[5]: Tải và lưu dạng TXT + Build EPUB (TXT -> HTML rồi EPUB)")
    print("[6]: Tải và lưu dạng HTML + TXT + Build EPUB")
    choice = input("Nhập lựa chọn (1-6): ").strip()

    do_epub = choice in {"4", "5", "6"}
    if do_epub:
        global EPUB_TARGET
        target_in = input(f"Chọn EPUB target (2=EPUB 2, 3=EPUB 3 compat) [{EPUB_TARGET[4]}]: ").strip()
        EPUB_TARGET = "epub3" if target_in == "3" else "epub2"
        print(f"EPUB target: {EPUB_TARGET}")
        if not HAS_PILLOW and EPUB_TARGET in ["epub2", "epub3"]:
            print("⚠ EPUB cover có thể không tối ưu nếu thiếu Pillow (JPEG/Resize).")

    print("\n--- 3. Handling Cover Image ---")
    cover_bytes, cover_ext, cover_mime = _load_cover_from_input(cover_in, StoryUrl)
    print(f"Cover status: {'OK' if cover_bytes else 'MISSING'} ({cover_mime})")

    out_dir     = os.path.join("output", _slugify_vi(title))
    epub_outdir = "output"
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(epub_outdir, exist_ok=True)
    print("Output dir:", out_dir)

    saved_htmls: List[str] = []
    saved_txts:  List[str] = []
    epub_path:   str | None = None

    if choice in {"1", "3", "4", "6"}:
        print(f"\n[Mode {choice}] Tải HTML…")
        saved_htmls = save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)
        print(f"✔ Đã lưu HTML: {len(saved_htmls)} files.")

    if choice in {"2", "3", "5", "6"}:
        if choice == "2":
            print("\n[Mode 2] Tải và lưu TXT trực tiếp…")
            saved_txts = save_all_chapters_to_txt(title, chapters, out_dir, start=1, end=None)
            print(f"✔ Đã lưu TXT: {len(saved_txts)} files.")
        elif choice in {"3", "5", "6"}:
            print(f"\n[Mode {choice}] Trích TXT từ HTML đã tải…")
            saved_txts = convert_htmls_to_txts(out_dir, only_paths=saved_htmls)
            print(f"✔ Đã lưu TXT: {len(saved_txts)} files.")

    if choice in {"4", "5", "6"}:
        print(f"\n[Mode {choice}] Build EPUB...")
        if not saved_htmls:
            print("⚠ Cần có file HTML để build EPUB. Bắt đầu tải HTML…")
            saved_htmls = save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)

        if saved_htmls:
            epub_path = build_epub(
                epub_outdir, title, author, saved_htmls,
                cover_bytes=cover_bytes, cover_ext=cover_ext, cover_mime=cover_mime,
                epub_target=EPUB_TARGET, tags=info.get("Genres")
            )
            print(f"✔ EPUB: {epub_path}")

    if choice in {"2", "5"}:
        print("\n🧹 Xóa file HTML trung gian (mode 2/5)…")
        _delete_files(saved_htmls)
        saved_htmls = []

    print("\n------------------- DONE -------------------")
    if epub_path:
        print(f"✅ Đã tạo EPUB: {epub_path}")
    if saved_htmls:
        print(f"✅ Đã lưu HTML tại: {out_dir}")
    if saved_txts:
        print(f"✅ Đã lưu TXT tại: {out_dir}")


try:
    from download_policy import install_adapter_policy as _install_adapter_policy
    _install_adapter_policy(globals())
except Exception:
    pass
if __name__ == "__main__":
    main()
