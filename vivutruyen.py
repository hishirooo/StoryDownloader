# -*- coding: utf-8 -*-
"""
vivutruyen2_downloader.py
Chỉ cần nhập URL truyện:
- Tự lấy thông tin truyện
- Tự lấy 5 chương đầu từ listing
- Tự lần theo dòng "ĐỌC TIẾP: ..." để tải các chương tiếp theo
- Tự làm sạch nội dung chương (loại link đọc tiếp, script, quảng cáo)
- Tự tải cover + convert JPEG nếu có Pillow
- Tự lưu HTML từng chương
- Tự build EPUB2
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Comment
from typing import Optional, List, Dict, Tuple
from urllib.parse import urljoin, urlparse
import requests, re, html, os, unicodedata, zipfile, time, datetime as dt, io
from download_logger import chapter_log_line
from epub_metadata import PUBLISHER, subject_xml

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
TIMEOUT = 25
SLEEP_BETWEEN_CHAPS = 0.2
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)
MAX_FOLLOW_CHAPTERS = 10000


try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("⚠ Không tìm thấy Pillow. Cover sẽ dùng ảnh gốc nếu không convert được.")


# =========================
# Helpers
# =========================
def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""


def _safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:150] or "chapter"


def _slugify_vi(s: str) -> str:
    s = (s or "book").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-_")
    return (s or "book")[:100]


def _normalize_url(url: str) -> str:
    url = html.unescape((url or "").strip())
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    parts = urlparse(url)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = re.sub(r"/+", "/", parts.path or "/").rstrip("/")
    if not path:
        path = "/"
    return f"{scheme}://{netloc}{path}"


def _same_story_path(url_a: str, url_b: str) -> bool:
    try:
        a = urlparse(url_a)
        b = urlparse(url_b)
        return a.netloc.replace("www.", "") == b.netloc.replace("www.", "") and a.path.strip("/") == b.path.strip("/")
    except Exception:
        return False


def _fetch_html(url: str, tries: int = 3, backoff: float = 0.8) -> BeautifulSoup:
    last_err = None
    for k in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code in RETRY_STATUS:
                if k == tries - 1:
                    r.raise_for_status()
                time.sleep(backoff * (k + 1))
                continue
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            soup = BeautifulSoup(r.text, "html.parser")
            soup.base_url = url
            return soup
        except Exception as e:
            last_err = e
            if k < tries - 1:
                time.sleep(backoff * (k + 1))
    raise last_err


# =========================
# Book info
# =========================
def _get_book_info(soup: BeautifulSoup, story_url: str) -> Dict[str, str]:
    info = {
        "title": "",
        "author": "Unknown",
        "genre": "N/A",
        "status": "N/A",
        "cover_url": "",
        "description": "",
    }

    selectors_title = [
        "h1.entry-title",
        "h1.card-title",
        ".post-title h1",
        "main h1",
        "article h1",
        "h1",
    ]
    for sel in selectors_title:
        n = soup.select_one(sel)
        if n and _text(n):
            info["title"] = _text(n)
            break
    if not info["title"]:
        info["title"] = _text(soup.title) or "Truyện"

    dts = soup.select("dt, .summary-heading")
    dds = soup.select("dd, .summary-content")
    if dts and dds:
        for i, dt_node in enumerate(dts):
            if i >= len(dds):
                continue
            key = _text(dt_node).lower()
            val = _text(dds[i])
            if "tác giả" in key and val:
                info["author"] = val
            elif "trạng thái" in key and val:
                info["status"] = val
            elif "thể loại" in key and val:
                info["genre"] = val

    if info["author"] == "Unknown" or info["genre"] == "N/A" or info["status"] == "N/A":
        for li in soup.select("ul.info-truyen li, .info-truyen li"):
            b = li.find("b")
            if not b:
                continue
            key = _text(b).lower().strip()
            li_clone = BeautifulSoup(str(li), "html.parser")
            b_clone = li_clone.find("b")
            if b_clone:
                b_clone.extract()
            links = li_clone.find_all("a")
            if links:
                val = ", ".join(_text(a) for a in links if _text(a))
            else:
                val = _text(li_clone).strip(" :|-")

            if "tác giả" in key and val:
                info["author"] = val
            elif "thể loại" in key and val:
                info["genre"] = val
            elif "trạng thái" in key and val:
                info["status"] = val

    if info["author"] == "Unknown" or info["genre"] == "N/A" or info["status"] == "N/A":
        for node in soup.find_all(["div", "li", "p", "span"]):
            txt = _text(node)
            if not txt or len(txt) > 250:
                continue
            low = txt.lower()

            if "tác giả" in low and info["author"] == "Unknown":
                m = re.search(r"tác\s*giả\s*:\s*([^\n|]+?)(?=\s*(thể\s*loại|trạng\s*thái)\s*:|$)", txt, re.I)
                if m and m.group(1).strip():
                    info["author"] = m.group(1).strip()

            if "thể loại" in low and info["genre"] == "N/A":
                m = re.search(r"thể\s*loại\s*:\s*([^\n|]+?)(?=\s*(trạng\s*thái|tác\s*giả)\s*:|$)", txt, re.I)
                if m and m.group(1).strip():
                    info["genre"] = m.group(1).strip()

            if "trạng thái" in low and info["status"] == "N/A":
                m = re.search(r"trạng\s*thái\s*:\s*([^\n|]+?)(?=\s*(thể\s*loại|tác\s*giả)\s*:|$)", txt, re.I)
                if m and m.group(1).strip():
                    info["status"] = m.group(1).strip()

    if info["genre"] == "N/A":
        tags = [a.get_text(" ", strip=True) for a in soup.select("a[rel='tag'], .genres-content a, .category a, .tags a")]
        tags = [x for x in tags if x]
        if tags:
            info["genre"] = " - ".join(dict.fromkeys(tags))

    cover_selectors = [
        ".image-truyen img",
        ".book-thumb img",
        ".summary_image img",
        ".detail-thumbnail img",
        ".book-cover img",
        ".entry-content img",
    ]
    for sel in cover_selectors:
        n = soup.select_one(sel)
        if not n:
            continue
        src = (
            n.get("data-src")
            or n.get("data-lazy-src")
            or n.get("data-original")
            or n.get("src")
            or ""
        ).strip()
        if not src:
            continue
        full = urljoin(story_url, src)
        low = full.lower()
        if "/themes/" in low or "/cache/" in low or "logo" in low or "icon" in low:
            continue
        info["cover_url"] = full
        break

    desc_selectors = [
        ".description-summary",
        ".summary__content",
        ".entry-content",
        ".post-content_item .summary-content",
    ]
    for sel in desc_selectors:
        n = soup.select_one(sel)
        if n and _text(n):
            info["description"] = _text(n)
            break

    return info


# =========================
# Chapter list from story page (seed list)
# =========================
def _extract_chapter_number(url_or_text: str) -> Optional[float]:
    s = url_or_text or ""
    m = re.search(r"chuong[-\s_/]*([0-9]+(?:[._-][0-9]+)?)", s, re.I)
    if not m:
        return None
    raw = m.group(1).replace("_", ".").replace("-", ".")
    try:
        return float(raw)
    except Exception:
        return None


def _get_list_chapters(soup: BeautifulSoup, story_url: str) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen = set()

    selectors = [
        "div.list-chapters a[href]",
        "div.episode-title a[href]",
        "li.wp-manga-chapter a[href]",
        "a[href*='/chuong-']",
    ]

    for sel in selectors:
        for a in soup.select(sel):
            href = a.get("href")
            title = _text(a)
            if not href:
                continue
            full = urljoin(story_url, href)
            if "/chuong-" not in full.lower():
                continue
            key = _normalize_url(full)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "title": title or os.path.basename(full.rstrip("/")),
                "url": full,
            })

    def sort_key(ch: Dict[str, str]):
        num = _extract_chapter_number(ch.get("url", "") + " " + ch.get("title", ""))
        if num is None:
            return (10**9, ch.get("url", ""))
        return (num, ch.get("url", ""))

    candidates = sorted(candidates, key=sort_key)
    return candidates

def _extract_all_chapter_links_from_soup(soup: BeautifulSoup, base_url: str) -> List[str]:
    urls: List[str] = []

    for a in soup.find_all("a", href=True):
        href = _normalize_url(urljoin(base_url, a["href"].strip()))
        if href and re.search(r"/chuong[-\s_]*\d+$", href, re.I):
            urls.append(href)

    raw_text = soup.get_text("\n", strip=True)
    for m in re.finditer(r'https?://[^\s\'"<>]+', raw_text, flags=re.I):
        u = _normalize_url(m.group(0).strip().rstrip(").,;]"))
        if u and re.search(r"/chuong[-\s_]*\d+$", u, re.I):
            urls.append(u)

    out: List[str] = []
    seen = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out



# =========================
# Chapter parsing + next pointer
# =========================
# =========================
# Chapter parsing + next pointer
# =========================
# Danh sách thể loại truyện – dùng để lọc dòng rác ngắn trùng tên thể loại
GENRE_TAGS: set[str] = {
    # Tiếng Việt
    "khoa huyễn", "nữ phụ", "mạt thế", "trinh thám", "huyền huyễn",
    "cổ đại", "hài hước", "hiện đại", "đô thị", "khác", "xuyên không",
    "cung đấu", "gia đấu", "điền văn", "đoản văn", "nữ cường",
    "hệ thống", "dị giới", "xuyên sách", "linh dị", "kinh dị",
    "ngôn linh", "tâm linh", "tâm lý", "kỳ ảo", "báo thù",
    "hợp đồng cá cược", "khoa học viễn tưởng", "giả tưởng khoa học",
    "đam mỹ", "tu tiên", "ngôn tình", "trọng sinh", "truyện teen",
    "truyện chữ", "truyện tranh", "sủng", "sắc", "tiểu thuyết",
    "lãng mạn", "ngược", "võ hiệp", "kiếm hiệp", "đồng nhân",
    "quân sự", "lịch sử", "Light Novel".lower(), "manga", "manhwa",
    "manhua", "webtoon", "one shot", "doujinshi",
    # Tiếng Anh (thường xuất hiện trên vivutruyen)
    "adult", "harem", "action", "adventure", "drama", "school life",
    "slice of life", "night owl", "boylove", "romance", "fantasy",
    "comedy", "horror", "mystery", "sci-fi", "supernatural",
    "shoujo", "shounen", "seinen", "josei", "smut", "ecchi",
    "yaoi", "yuri", "gender bender", "martial arts", "mecha",
    "psychological", "tragedy", "mature",
}

JUNK_LINE_PATTERNS = [
    r"^\s*ĐỌC\s*TIẾP\s*:\s*https?://\S+\s*$",
    r"^\s*https?://\S+\s*$",
    r"tiktok\.com",
    r"s\.shopee\.vn",
    r"mời\s+quý\s+độc\s+giả",
    r"click\b",
    r"nguồn\s*:",
    # Menu / navigation rác
    r"^(Đăng Ký|Đăng Nhập|Thoát|Thống Kê|Đề Cử|Xem Nhiều|Mới Cập Nhật|Mới Nhất|Thể Loại|Ngược|Ngôn Tình|Truyện Teen|Shoujo|Truyện Chữ|Trọng Sinh|Truyện Tranh|Sủng|Sắc|Smut|Tiểu thuyết|Prev|Next|Trang chủ|Tài Khoản|Home|Scroll Up|Khám phá thêm)$",
    # Dòng "Chương" trơ trọi
    r"^Chương\s*$",
    # Footer rác vivutruyen
    r"Website đang trong quá trình thử nghiệm",
    # Breadcrumb rác
    r"^Home\s*/",
]


def _remove_unwanted_tags(node):
    """Loại bỏ tất cả các thẻ HTML rác trước khi trích xuất text."""
    # 1. Thẻ không liên quan đến nội dung
    for tag in node.find_all(["script", "style", "noscript", "iframe",
                              "svg", "canvas", "form", "nav", "header",
                              "footer"]):
        tag.decompose()

    # 2. Comment HTML
    for c in node.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    # 3. Google-anno-skip (block quảng cáo thể loại chèn giữa nội dung)
    for div in node.find_all("div", class_="google-anno-skip"):
        div.decompose()

    # 4. Navigation bar, breadcrumb, nút prev/next chapter
    junk_selectors = [
        ".uk-navbar", ".uk-navbar-nav", ".uk-breadcrumb",
        "[class*='breadcrumb']",
        ".prev_chap", ".next_chap",
        "a.prev_chap", "a.next_chap",
        "[class*='chapter-nav']", "[class*='chap-nav']",
        ".chapter-button", ".chapter-btn",
        "[id='invisible-link']",
        ".social-share", ".share-buttons",
    ]
    for sel in junk_selectors:
        for el in node.select(sel):
            el.decompose()

    # 5. Xóa các button (nút Prev/Next)
    for btn in node.find_all("button"):
        btn.decompose()
    for a_tag in node.find_all("a", class_=lambda c: c and "uk-button" in c):
        a_tag.decompose()

    # 6. Xóa các div chứa danh sách link thể loại (nhiều link ngắn liên tiếp)
    for div in node.find_all("div"):
        links = div.find_all("a")
        if len(links) >= 5:
            # Nếu div chứa ≥5 link và hầu hết text ngắn → đây là block thể loại
            short_links = sum(1 for a in links if len(a.get_text(strip=True)) < 25)
            if short_links >= len(links) * 0.7:
                div.decompose()


def _find_content_node(soup: BeautifulSoup):
    selectors = [
        "#chapter-content-render",
        ".reading-content",
        ".entry-content",
        ".text-left",
        "article .entry-content",
        ".chapter-content",
        "main",
    ]
    for sel in selectors:
        n = soup.select_one(sel)
        if n and len(n.get_text(" ", strip=True)) > 100:
            return n

    divs = sorted(
        soup.find_all("div"),
        key=lambda d: len(d.get_text(" ", strip=True)),
        reverse=True,
    )
    return divs[0] if divs else soup


def _pick_chapter_title(soup: BeautifulSoup, fallback: str = "Chương") -> str:
    selectors = [
        "#chapter-heading",
        "h1.entry-title",
        "h1.card-title",
        "main h1",
        "h1",
        "h2",
    ]
    for sel in selectors:
        n = soup.select_one(sel)
        t = _text(n)
        if t:
            return t
    return fallback


def _find_next_url(content_node, raw_text: str, base_url: str, story_url: str) -> Optional[str]:
    # Ưu tiên regex text kiểu: ĐỌC TIẾP : https://...
    m = re.search(r"ĐỌC\s*TIẾP\s*:\s*(https?://\S+)", raw_text, flags=re.I)
    if m:
        return _normalize_url(m.group(1).strip())

    # Tìm trong các thẻ a
    for a in content_node.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        txt = a.get_text(" ", strip=True)
        if "đọc tiếp" in txt.lower() and "/chuong-" in href.lower():
            return _normalize_url(href)

    # fallback: tìm link chương lớn nhất trong content
    chapter_links = []
    for a in content_node.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if "/chuong-" in href.lower():
            chapter_links.append(href)
    if chapter_links:
        chapter_links = sorted(set(chapter_links), key=lambda u: (_extract_chapter_number(u) is None, _extract_chapter_number(u) or 10**9, u))
        return _normalize_url(chapter_links[-1])

    return None


def _clean_text_lines(text: str) -> List[str]:
    text = text.replace("\r", "")
    lines = [ln.strip() for ln in text.split("\n")]
    cleaned: List[str] = []
    for line in lines:
        if not line:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue

        # Kiểm tra regex junk patterns
        bad = False
        for pat in JUNK_LINE_PATTERNS:
            if re.search(pat, line, re.I):
                bad = True
                break
        if bad:
            continue

        # Kiểm tra dòng ngắn trùng tên thể loại (case-insensitive)
        if line.lower().strip() in GENRE_TAGS:
            continue

        # Lọc dòng quá ngắn chỉ chứa 1-2 từ không phải nội dung truyện
        # (ví dụ: tên menu, nút bấm, label…)
        stripped_lower = line.lower().strip()
        if len(stripped_lower) <= 3 and not stripped_lower[0].isdigit():
            # Bỏ qua các dòng ≤3 ký tự không phải số chương ("1.", "2.", v.v.)
            continue

        cleaned.append(line)

    # bỏ dòng trống thừa đầu/cuối
    while cleaned and cleaned[0] == "":
        cleaned.pop(0)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return cleaned


def fetch_chapter_content(url: str, story_url: str) -> Dict[str, str]:
    soup = _fetch_html(url)
    title = _pick_chapter_title(soup)
    content_node = _find_content_node(soup)

    # ⚠ Lấy next_url + discovered_links TRƯỚC KHI dọn rác HTML,
    #   vì _remove_unwanted_tags sẽ xóa luôn các link navigation/ĐỌC TIẾP.
    raw_text_for_nav = content_node.get_text("\n", strip=True).replace("\xa0", " ")
    next_url = _find_next_url(content_node, raw_text_for_nav, url, story_url)
    discovered_links = _extract_all_chapter_links_from_soup(soup, url)

    # Bây giờ mới dọn rác để lấy nội dung sạch
    _remove_unwanted_tags(content_node)
    raw_text = content_node.get_text("\n", strip=True).replace("\xa0", " ")

    raw_text = re.sub(r"\n?\s*ĐỌC\s*TIẾP\s*:\s*https?://\S+.*$", "", raw_text, flags=re.I | re.S)
    lines = _clean_text_lines(raw_text)
    parts: List[str] = []
    
    # Chuẩn hóa tên truyện để so sánh (dùng để phát hiện và xóa tên truyện bị lặp)
    clean_title = (title or "").strip().lower()

    for ln in lines:
        if not ln:
            continue
            
        # Nếu dòng hiện tại giống hệt tên truyện thì bỏ qua (không lưu vào nội dung)
        if clean_title and ln.lower() == clean_title:
            continue
            
        safe = html.escape(ln)
        parts.append(f"<p>{safe}</p>")

    content_html = "\n".join(parts) if parts else "<p>(Trống)</p>"
    return {
        "title": title or "Chương",
        "content_html": content_html,
        "url": url,
        "next_url": next_url or "",
        "discovered_links": discovered_links,
    }


# =========================
# Follow next chain
# =========================
def _collect_all_chapters(story_url: str, seed_chapters: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not seed_chapters:
        return []

    chapter_map: Dict[str, Dict[str, str]] = {}
    order_urls: List[str] = []

    for ch in seed_chapters:
        u = _normalize_url(ch["url"])
        if u not in chapter_map:
            chapter_map[u] = {"title": ch.get("title", ""), "url": ch["url"]}
            order_urls.append(u)

    # lần theo từ chapter cuối cùng có sẵn trong listing
    current_url = _normalize_url(seed_chapters[-1]["url"])
    visited_follow = set(order_urls)

    while current_url and len(order_urls) < MAX_FOLLOW_CHAPTERS:
        try:
            chap = fetch_chapter_content(current_url, story_url)
            next_url = _normalize_url(chap.get("next_url", ""))
            if not next_url:
                break
            if next_url in visited_follow:
                break
            if "/chuong-" not in next_url.lower():
                break

            chapter_map[next_url] = {
                "title": chap.get("next_url", "").split("/")[-1].replace("-", " ").title(),
                "url": next_url,
            }
            order_urls.append(next_url)
            visited_follow.add(next_url)
            current_url = next_url
            print(f"[FOLLOW] + {next_url}", flush=True)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"⚠ Lỗi follow next từ {current_url}: {e}", flush=True)
            break

    # sort lại bằng số chương nếu parse được
    chapters = list(chapter_map.values())
    chapters = sorted(
        chapters,
        key=lambda ch: (
            _extract_chapter_number(ch.get("url", "") + " " + ch.get("title", "")) is None,
            _extract_chapter_number(ch.get("url", "") + " " + ch.get("title", "")) or 10**9,
            ch.get("url", ""),
        ),
    )
    return chapters


# =========================
# HTML save
# =========================
HTML_TEMPLATE = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{doc_title}</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.75;max-width:860px;margin:2rem auto;padding:0 1rem;background:#f4f4f6;color:#222}}
    h1{{font-size:1.6rem;margin:0 0 1rem}}
    .meta{{color:#666;font-size:.92rem;margin-bottom:1rem}}
    article{{background:#fff;border-radius:12px;padding:1rem 1.2rem;box-shadow:0 1px 10px rgba(0,0,0,.06)}}
    p{{margin:.75rem 0}}
    img{{max-width:100%;height:auto}}
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
    path = os.path.join(out_dir, fname)
    html_out = HTML_TEMPLATE.format(
        doc_title=f"{book_title} - {chap.get('title') or f'Chương {chapter_idx}'}",
        chapter_title=html.escape(chap.get('title') or f"Chương {chapter_idx}"),
        book_title=html.escape(book_title or "Truyện"),
        src=chap.get("url") or "",
        content=chap.get("content_html") or "<p>(Không có nội dung)</p>",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_out)
    return path


def save_all_chapters_to_html(book_title: str, chapters: List[Dict[str, str]], out_dir: str) -> List[str]:
    total = len(chapters)
    saved: List[str] = []

    for i, info in enumerate(chapters, start=1):
        try:
            chap = fetch_chapter_content(info["url"], story_url="")
            if not chap.get("title") and info.get("title"):
                chap["title"] = info["title"]
            p = save_chapter_html(book_title, i, chap, out_dir)
            print(chapter_log_line(i, total, chap.get("status_code", 200), i, total, chap.get("title") or info.get("title") or ""), flush=True)
            saved.append(p)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(chapter_log_line(i, total, "ERR", i, total, f"{info.get('title') or info.get('url')} ({e})"), flush=True)
    return saved


# =========================
# Cover helpers
# =========================
def _sniff_image_type(data: bytes):
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


def _ensure_jpeg_cover(img_bytes: bytes):
    if not HAS_PILLOW:
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)

    try:
        im = Image.open(io.BytesIO(img_bytes))
        resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, resample)

        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")

        out = io.BytesIO()
        im.save(out, format="JPEG", quality=90, optimize=True)
        return (out.getvalue(), ".jpg", "image/jpeg")
    except Exception:
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)


def _load_cover_from_web(story_url: str, cover_url: str):
    try:
        if not cover_url:
            soup = _fetch_html(story_url)
            cover_url = _get_book_info(soup, story_url).get("cover_url", "")
        if cover_url:
            print(f"Tự lấy cover: {cover_url}")
            r = requests.get(cover_url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return _ensure_jpeg_cover(r.content)
    except Exception as e:
        print(f"⚠ Không lấy được cover: {e}")
    return (None, None, None)


# =========================
# EPUB2 builder
# =========================
def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)


def build_epub2_from_htmls(
    html_paths: List[str],
    book_title: str,
    author: str,
    out_dir: str,
    cover_bytes: Optional[bytes] = None,
    cover_ext: Optional[str] = None,
    cover_mime: Optional[str] = None,
    genre: Optional[str] = None,
) -> str:
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"{_slugify_vi(book_title)}.epub")

    items = []
    for html_path in html_paths:
        with open(html_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        title_node = soup.find("h1") or soup.find("title")
        title = title_node.get_text(strip=True) if title_node else os.path.basename(html_path)
        node = soup.select_one("article") or soup.body or soup
        content_html = "".join(str(x) for x in node.children)
        items.append({"title": title, "content_html": content_html})

    book_id = f"urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}"
    cover_href = f"images/cover{cover_ext}" if cover_bytes and cover_ext else None
    subjects = subject_xml(genre, indent="    ")

    with zipfile.ZipFile(out_file, "w") as z:
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""".encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)

        css = b"body{font-family:serif;line-height:1.6;margin:5%;} h1{text-align:center;} img{max-width:100%;height:auto;}"
        _epub_write(z, "OEBPS/style.css", css)

        manifest_items = [
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '<item id="style" href="style.css" media-type="text/css"/>',
        ]
        spine_items = []
        nav_points = []

        if cover_href:
            z.writestr(
                "OEBPS/cover.xhtml",
                f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Cover</title></head>
<body><div><img src="{cover_href}" alt="cover"/></div></body>
</html>""".encode("utf-8"),
            )
            _epub_write(z, f"OEBPS/{cover_href}", cover_bytes)
            manifest_items.append('<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>')
            manifest_items.append(f'<item id="cover-image" href="{cover_href}" media-type="{cover_mime or "image/jpeg"}"/>')
            spine_items.append('<itemref idref="cover" linear="yes"/>')

        for i, item in enumerate(items, start=1):
            chap_name = f"chap_{i:04d}.xhtml"
            chap_id = f"chap{i}"
            chap_html = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{html.escape(item['title'])}</title>
  <link href="style.css" rel="stylesheet" type="text/css"/>
</head>
<body>
  <h1>{html.escape(item['title'])}</h1>
  {item['content_html']}
</body>
</html>"""
            _epub_write(z, f"OEBPS/{chap_name}", chap_html.encode("utf-8"))
            manifest_items.append(f'<item id="{chap_id}" href="{chap_name}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="{chap_id}"/>')
            nav_points.append((i, chap_name, item["title"]))

        ncx_points = []
        play_order = 1
        if cover_href:
            ncx_points.append(f"""
    <navPoint id="nav-cover" playOrder="{play_order}">
      <navLabel><text>Cover</text></navLabel>
      <content src="cover.xhtml"/>
    </navPoint>""")
            play_order += 1

        for _, chap_name, chap_title in nav_points:
            ncx_points.append(f"""
    <navPoint id="nav-{play_order}" playOrder="{play_order}">
      <navLabel><text>{html.escape(chap_title)}</text></navLabel>
      <content src="{chap_name}"/>
    </navPoint>""")
            play_order += 1

        toc_ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{book_id}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(book_title)}</text></docTitle>
  <navMap>
    {''.join(ncx_points)}
  </navMap>
</ncx>"""
        _epub_write(z, "OEBPS/toc.ncx", toc_ncx.encode("utf-8"))

        metadata = f"""
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{html.escape(book_title)}</dc:title>
    <dc:creator>{html.escape(author or '—')}</dc:creator>
    <dc:language>vi</dc:language>
    <dc:identifier id="BookId">{book_id}</dc:identifier>
    <dc:date>{dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</dc:date>
    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>
{subjects.rstrip()}
    <meta name="cover" content="cover-image"/>
  </metadata>""" if cover_href else f"""
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{html.escape(book_title)}</dc:title>
    <dc:creator>{html.escape(author or '—')}</dc:creator>
    <dc:language>vi</dc:language>
    <dc:identifier id="BookId">{book_id}</dc:identifier>
    <dc:date>{dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</dc:date>
    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>
{subjects.rstrip()}
  </metadata>"""

        opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId" version="2.0">
{metadata}
  <manifest>
    {''.join(manifest_items)}
  </manifest>
  <spine toc="ncx">
    {''.join(spine_items)}
  </spine>
</package>"""
        _epub_write(z, "OEBPS/content.opf", opf.encode("utf-8"))

    return out_file


# =========================
# Main flow
# =========================
def download_html_and_build_epub2(story_url: str):
    story_url = story_url.strip()
    soup = _fetch_html(story_url)
    info = _get_book_info(soup, story_url)

    title = info.get("title") or "Truyện"
    author = info.get("author") or "—"
    genre = info.get("genre") or "N/A"
    status = info.get("status") or "N/A"

    print("----------------- THÔNG TIN TRUYỆN -----------------")
    print(f"Title : {title}")
    print(f"Author: {author}")
    print(f"Genre : {genre}")
    print(f"Status: {status}")
    print("----------------------------------------------------")

    seed_chapters = _get_list_chapters(soup, story_url)
    if not seed_chapters:
        raise ValueError("Không tìm thấy danh sách chương ban đầu.")

    print(f"Seed chapters từ listing: {len(seed_chapters)}")
    chapters = _collect_all_chapters(story_url, seed_chapters)
    if not chapters:
        raise ValueError("Không gom được chương nào.")

    print(f"Total chapters sau follow ĐỌC TIẾP: {len(chapters)}")

    cover_bytes, cover_ext, cover_mime = _load_cover_from_web(story_url, info.get("cover_url", ""))
    print(f"Cover status: {'OK' if cover_bytes else 'MISSING'} ({cover_mime or '-'})")

    book_slug = _slugify_vi(title)
    html_out_dir = os.path.join("output", book_slug)
    epub_out_dir = "output"

    os.makedirs(html_out_dir, exist_ok=True)
    os.makedirs(epub_out_dir, exist_ok=True)

    print("\n[1/2] Tải HTML...")
    html_paths = save_all_chapters_to_html(title, chapters, html_out_dir)
    if not html_paths:
        raise ValueError("Không tải được HTML chương nào.")

    print(f"✔ Đã lưu HTML: {len(html_paths)} file(s)")

    print("\n[2/2] Build EPUB2...")
    epub_path = build_epub2_from_htmls(
        html_paths=html_paths,
        book_title=title,
        author=author,
        out_dir=epub_out_dir,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime,
        genre=genre,
    )

    print("\n------------------- DONE -------------------")
    print(f"✅ EPUB: {epub_path}")
    print(f"✅ HTML: {html_out_dir}")


def main():
    story_url = input("Nhập URL truyện: ").strip()
    if not story_url:
        print("❌ URL trống.")
        return

    try:
        download_html_and_build_epub2(story_url)
    except Exception as e:
        print(f"❌ Lỗi: {e}")



try:
    from download_policy import install_adapter_policy as _install_adapter_policy
    _install_adapter_policy(globals())
except Exception:
    pass
if __name__ == "__main__":
    main()
