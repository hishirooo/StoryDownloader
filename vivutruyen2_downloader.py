# -*- coding: utf-8 -*-
"""
vivutruyen2_downloader_improved.py

Mục tiêu:
- Chỉ cần nhập URL truyện
- Tự lấy info truyện
- Tự lấy danh sách chương từ trang truyện
- Tự follow link "ĐỌC TIẾP" hoặc chương kế tiếp
- Làm sạch nội dung mạnh hơn, tránh hút menu / footer / Prev / Next / category
- Mỗi chương chỉ fetch 1 lần, không tải lặp lại khi save HTML
- Chống trùng chapter theo URL + chapter number + slug path
- Lưu HTML từng chương + build EPUB2
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Comment, Tag
from typing import Optional, List, Dict, Tuple
from urllib.parse import urljoin, urlparse, urlunparse
import requests
import re
import html
import os
import unicodedata
import zipfile
import time
import datetime as dt
import io
import copy

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
TIMEOUT = 25
SLEEP_BETWEEN_CHAPS = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)
MAX_FOLLOW_CHAPTERS = 10000
MIN_CONTENT_TEXT_LEN = 120

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
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url

    p = urlparse(url)
    scheme = p.scheme or "https"
    netloc = p.netloc.lower().replace("www.", "")
    path = re.sub(r"/+", "/", p.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((scheme, netloc, path, "", p.query, ""))


def _normalized_netloc(url: str) -> str:
    try:
        return urlparse(_normalize_url(url)).netloc
    except Exception:
        return ""


def _path_key(url: str) -> str:
    try:
        return urlparse(_normalize_url(url)).path.strip("/").lower()
    except Exception:
        return ""


def _same_story_path(url_a: str, url_b: str) -> bool:
    try:
        a = urlparse(_normalize_url(url_a))
        b = urlparse(_normalize_url(url_b))
        return a.netloc == b.netloc and a.path.strip("/") == b.path.strip("/")
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

    # Kiểu dt/dd hoặc summary-heading/summary-content
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

    # Fallback cho dạng:
    # <ul class="info-truyen ...">
    #   <li><b>Tác giả:</b> ...</li>
    #   <li><b>Thể Loại:</b> <a>Hiện đại</a></li>
    #   <li><b>Trạng Thái:</b> Hoàn thành</li>
    if info["author"] == "Unknown" or info["genre"] == "N/A" or info["status"] == "N/A":
        for li in soup.select("ul.info-truyen li"):
            b = li.find("b")
            if not b:
                continue

            key = _text(b).lower().strip()

            # clone li để bỏ thẻ <b> rồi lấy phần value còn lại
            li_clone = BeautifulSoup(str(li), "html.parser")
            b_clone = li_clone.find("b")
            if b_clone:
                b_clone.extract()

            # nếu có link thì ưu tiên lấy text từ link
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

    if info["genre"] == "N/A":
        tags = [a.get_text(" ", strip=True) for a in soup.select("a[rel='tag'], .genres-content a, .category a, .tags a")]
        tags = [x for x in tags if x]
        if tags:
            info["genre"] = " - ".join(dict.fromkeys(tags))

    cover_selectors = [
        ".image-truyen img",
        ".summary_image img",
        "img.img-fluid",
        ".book-cover img",
        ".entry-content img",
        "img",
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
        )
        if not src:
            continue
        src_lower = src.lower()
        if any(x in src_lower for x in ["logo", "icon", "avatar", "banner"]):
            continue
        info["cover_url"] = urljoin(story_url, src)
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
# Chapter helpers
# =========================
def _extract_chapter_number(url_or_text: str) -> Optional[float]:
    s = (url_or_text or "").lower()

    patterns = [
        r"chuong[-\s_/.:]*([0-9]+(?:[._-][0-9]+)?)",
        r"chương[-\s_/.:]*([0-9]+(?:[._-][0-9]+)?)",
        r"chapter[-\s_/.:]*([0-9]+(?:[._-][0-9]+)?)",
    ]
    for pat in patterns:
        m = re.search(pat, s, re.I)
        if m:
            raw = m.group(1).replace("_", ".").replace("-", ".")
            try:
                return float(raw)
            except Exception:
                pass
    return None


def _chapter_slug_key(url: str) -> str:
    path = _path_key(url)
    m = re.search(r"(chuong[-\w.]+)$", path, re.I)
    return (m.group(1).lower() if m else path)


def _looks_like_chapter_url(url: str) -> bool:
    u = (url or "").lower()
    return "/chuong-" in u or "/chương-" in u or re.search(r"/chapter[-_/]", u) is not None


def _chapter_sort_key(ch: Dict[str, str]):
    num = ch.get("chapter_no")
    if num is None:
        num = _extract_chapter_number((ch.get("url") or "") + " " + (ch.get("title") or ""))
    return (num is None, num if num is not None else 10**9, ch.get("url", ""))


# =========================
# Get initial chapter list
# =========================
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
            if not _looks_like_chapter_url(full):
                continue
            key = _normalize_url(full)
            if key in seen:
                continue
            seen.add(key)
            chapter_no = _extract_chapter_number(full + " " + title)
            candidates.append({
                "title": title or os.path.basename(full.rstrip("/")),
                "url": full,
                "chapter_no": chapter_no,
            })

    return sorted(candidates, key=_chapter_sort_key)


# =========================
# Content extraction
# =========================
JUNK_LINE_PATTERNS = [
    r"^\s*ĐỌC\s*TIẾP\s*:\s*https?://\S+\s*$",
    r"^\s*https?://\S+\s*$",
    r"tiktok\.com",
    r"s\.shopee\.vn",
    r"mời\s+quý\s+độc\s+giả",
    r"click\b",
    r"nguồn\s*:",
    r"^\s*prev\s*$",
    r"^\s*next\s*$",
    r"^\s*đăng\s*ký\s*$",
    r"^\s*đăng\s*nhập\s*$",
    r"^\s*tài\s*khoản\s*$",
    r"^\s*trang\s*chủ\s*$",
    r"^\s*thể\s*loại\s*$",
    r"^\s*đề\s*cử\s*$",
    r"^\s*xem\s*nhiều\s*$",
    r"^\s*mới\s*cập\s*nhật\s*$",
    r"^\s*mới\s*nhất\s*$",
    r"^\s*website\s+đang\s+trong\s+quá\s+trình\s+thử\s+nghiệm\s*$",
    r"^\s*quay\s+lại\s+chương\s+\d+\s*:?[\s]*$",
    r"^\s*chương\s+\d+\s*$",
    r"^\s*chuong\s+\d+\s*$",
    r"^\s*\d+\s*$",
]

BAD_EXACT_LINES = {
    "cập nhật", "mới nhất", "thể loại", "ngược", "ngôn tình", "truyện teen",
    "shoujo", "truyện chữ", "trọng sinh", "truyện tranh", "sủng", "sắc",
    "smut", "tiểu thuyết", "khoa huyễn", "nữ phụ", "school life",
    "slice of life", "mạt thế", "night owl", "trinh thám", "huyền huyễn",
    "cổ đại", "hài hước", "hiện đại", "đô thị", "khác", "xuyên không",
    "cung đấu", "gia đấu", "adult", "harem", "manhwa", "điền văn",
    "đoản văn", "nữ cường", "action", "hệ thống", "adventure", "drama",
    "dị giới", "xuyên sách", "linh dị", "kinh dị", "ngôn linh", "tâm linh",
    "tâm lý", "kỳ ảo", "báo thù", "khoa học viễn tưởng", "boylove", "đam mỹ",
    "tu tiên", "tiên giới", "tiên hiệp", "huyền ảo", "giả tưởng khoa học",
    "hợp đồng cá cược",
}

REMOVE_SELECTORS = [
    "script", "style", "noscript", "iframe", "svg", "canvas", "form",
    "header", "footer", "nav", "aside",
    ".sharedaddy", ".ads", ".ad", ".advertisement", ".banner", ".breadcrumbs",
    ".social-share", ".related-posts", ".related", ".tags", ".tagcloud",
    ".entry-meta", ".post-navigation", ".navigation", ".nav-links",
    ".wp-block-buttons", ".wp-block-button", ".mvp-post-soc-wrap",
    ".ez-toc-container", ".code-block", ".code-block-1", ".code-block-2",
    ".jp-relatedposts", ".post-tags", ".sidebar", ".widget",
    ".comment-respond", ".comments-area", ".quads-location",
]

CONTENT_SELECTORS = [
    "#chapter-content-render",
    ".reading-content",
    "article .chapter-content",
    ".chapter-content",
    "article .entry-content",
    ".entry-content",
    ".text-left",
    "article",
]


def _remove_unwanted_tags(node: Tag) -> None:
    for sel in REMOVE_SELECTORS:
        for tag in node.select(sel):
            tag.decompose()

    for tag in node.find_all(["script", "style", "noscript", "iframe", "svg", "canvas", "form"]):
        tag.decompose()

    for c in node.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    for tag in node.find_all(attrs={"hidden": True}):
        tag.decompose()

    for tag in node.find_all(style=True):
        style = (tag.get("style") or "").lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            tag.decompose()


def _score_content_node(node: Tag) -> Tuple[int, int, int]:
    text = node.get_text(" ", strip=True).replace("\xa0", " ")
    text_len = len(text)
    p_count = len(node.find_all("p"))
    br_count = len(node.find_all("br"))
    bad_hits = 0
    lower_text = text.lower()
    for token in ["đăng ký", "đăng nhập", "prev", "next", "website đang trong quá trình thử nghiệm", "thể loại"]:
        if token in lower_text:
            bad_hits += 1
    return (text_len - bad_hits * 300, p_count, br_count)


def _find_content_node(soup: BeautifulSoup) -> Tag:
    candidates: List[Tag] = []

    for sel in CONTENT_SELECTORS:
        for n in soup.select(sel):
            if isinstance(n, Tag):
                txt = n.get_text(" ", strip=True)
                if len(txt) >= MIN_CONTENT_TEXT_LEN:
                    candidates.append(n)

    if not candidates:
        for n in soup.find_all(["article", "section", "div", "main"]):
            if not isinstance(n, Tag):
                continue
            txt = n.get_text(" ", strip=True)
            if len(txt) >= MIN_CONTENT_TEXT_LEN:
                candidates.append(n)

    if not candidates:
        return soup.body or soup

    best = max(candidates, key=_score_content_node)
    return best


def _pick_chapter_title(soup: BeautifulSoup, fallback: str = "Chương") -> str:
    selectors = [
        "#chapter-heading",
        "h1.entry-title",
        "h1.card-title",
        "main h1",
        "article h1",
        "h1",
        "h2",
    ]
    for sel in selectors:
        n = soup.select_one(sel)
        t = _text(n)
        if t:
            return t
    return fallback


def _is_junk_line(line: str) -> bool:
    s = (line or "").strip().replace("\xa0", " ")
    if not s:
        return True

    lower = s.lower()

    for pat in JUNK_LINE_PATTERNS:
        if re.search(pat, s, re.I):
            return True

    if lower in BAD_EXACT_LINES:
        return True

    if len(s) <= 3 and re.fullmatch(r"\d+", s):
        return True

    if len(s) <= 18 and lower in {"prev", "next", "hết"}:
        return lower != "hết"

    # menu / category lines thường rất ngắn và không có dấu câu kết câu
    if len(s) <= 22 and not re.search(r"[.!?…,:;”\"]$", s):
        if lower in BAD_EXACT_LINES:
            return True

    return False


def _paragraphs_from_node(content_node: Tag) -> List[str]:
    paragraphs: List[str] = []

    p_tags = content_node.find_all("p")
    if p_tags:
        for p in p_tags:
            txt = p.get_text(" ", strip=True).replace("\xa0", " ")
            txt = re.sub(r"\s+", " ", txt).strip()
            if not txt or _is_junk_line(txt):
                continue
            paragraphs.append(txt)
    else:
        raw_text = content_node.get_text("\n", strip=True).replace("\xa0", " ")
        raw_text = re.sub(r"\n?\s*ĐỌC\s*TIẾP\s*:\s*https?://\S+.*$", "", raw_text, flags=re.I | re.S)
        for ln in raw_text.split("\n"):
            txt = re.sub(r"\s+", " ", ln).strip()
            if not txt or _is_junk_line(txt):
                continue
            paragraphs.append(txt)

    cleaned: List[str] = []
    seen_tail = set()
    for txt in paragraphs:
        key = txt.lower()
        # chặn trùng đoạn do site render lặp
        if key in seen_tail and len(txt) < 50:
            continue
        seen_tail.add(key)
        cleaned.append(txt)

    # cắt từ dòng đọc tiếp trở xuống nếu vẫn còn sót
    stop_idx = None
    for i, txt in enumerate(cleaned):
        if re.search(r"đọc\s*tiếp", txt, re.I):
            stop_idx = i
            break
    if stop_idx is not None:
        cleaned = cleaned[:stop_idx]

    return cleaned


def _find_next_url(content_node: Tag, raw_text: str, base_url: str, story_url: str) -> Optional[str]:
    story_netloc = _normalized_netloc(story_url)
    current_num = _extract_chapter_number(base_url)

    m = re.search(r"ĐỌC\s*TIẾP\s*:\s*(https?://\S+)", raw_text, flags=re.I)
    if m:
        return _normalize_url(m.group(1).strip())

    best_candidate: Tuple[float, str] | None = None

    for a in content_node.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        href_n = _normalize_url(href)
        if not _looks_like_chapter_url(href_n):
            continue
        if story_netloc and _normalized_netloc(href_n) and _normalized_netloc(href_n) != story_netloc:
            # vẫn cho phép đổi giữa vivutruyen.net / vivutruyen2.net nếu path chapter hợp lệ
            if not any(host in _normalized_netloc(href_n) for host in ["vivutruyen.net", "vivutruyen2.net"]):
                continue

        txt = a.get_text(" ", strip=True)
        if "đọc tiếp" in txt.lower():
            return href_n

        num = _extract_chapter_number(href_n + " " + txt)
        if num is None:
            continue
        if current_num is not None and num < current_num:
            continue
        if best_candidate is None or num > best_candidate[0]:
            best_candidate = (num, href_n)

    if best_candidate:
        return best_candidate[1]

    return None


def fetch_chapter_content(url: str, story_url: str) -> Dict[str, str]:
    soup = _fetch_html(url)
    title = _pick_chapter_title(soup)
    source_node = _find_content_node(soup)
    content_node = copy.copy(source_node)
    # deep copy qua parse lại html để tránh decompose làm side-effect
    content_node = BeautifulSoup(str(source_node), "html.parser")
    working_node = content_node.find() or content_node

    _remove_unwanted_tags(working_node)

    raw_text = working_node.get_text("\n", strip=True).replace("\xa0", " ")
    next_url = _find_next_url(working_node, raw_text, url, story_url)
    paragraphs = _paragraphs_from_node(working_node)

    parts = [f"<p>{html.escape(p)}</p>" for p in paragraphs if p]
    content_html = "\n".join(parts) if parts else "<p>(Trống)</p>"

    chapter_no = _extract_chapter_number(url + " " + (title or ""))
    return {
        "title": title or "Chương",
        "content_html": content_html,
        "url": url,
        "next_url": next_url or "",
        "chapter_no": chapter_no,
        "paragraph_count": len(paragraphs),
    }


# =========================
# Collect all chapters without re-fetching later
# =========================
def _register_chapter(
    chapters_by_url: Dict[str, Dict[str, str]],
    chapter_num_map: Dict[float, str],
    slug_map: Dict[str, str],
    chap: Dict[str, str],
) -> bool:
    url_n = _normalize_url(chap.get("url", ""))
    if not url_n:
        return False

    chap = dict(chap)
    chap["url"] = url_n
    chap_no = chap.get("chapter_no")
    if chap_no is None:
        chap_no = _extract_chapter_number(url_n + " " + chap.get("title", ""))
        chap["chapter_no"] = chap_no

    slug_key = _chapter_slug_key(url_n)

    if url_n in chapters_by_url:
        old = chapters_by_url[url_n]
        if not old.get("content_html") and chap.get("content_html"):
            chapters_by_url[url_n] = chap
        return False

    if chap_no is not None and chap_no in chapter_num_map:
        old_url = chapter_num_map[chap_no]
        old = chapters_by_url.get(old_url, {})
        old_len = len(old.get("content_html", ""))
        new_len = len(chap.get("content_html", ""))
        if new_len > old_len:
            chapters_by_url.pop(old_url, None)
            chapters_by_url[url_n] = chap
            chapter_num_map[chap_no] = url_n
            slug_map[slug_key] = url_n
        return False

    if slug_key and slug_key in slug_map:
        old_url = slug_map[slug_key]
        old = chapters_by_url.get(old_url, {})
        old_len = len(old.get("content_html", ""))
        new_len = len(chap.get("content_html", ""))
        if new_len > old_len:
            chapters_by_url.pop(old_url, None)
            chapters_by_url[url_n] = chap
            if chap_no is not None:
                chapter_num_map[chap_no] = url_n
            slug_map[slug_key] = url_n
        return False

    chapters_by_url[url_n] = chap
    if chap_no is not None:
        chapter_num_map[chap_no] = url_n
    if slug_key:
        slug_map[slug_key] = url_n
    return True


def _collect_all_chapters(story_url: str, seed_chapters: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not seed_chapters:
        return []

    chapters_by_url: Dict[str, Dict[str, str]] = {}
    chapter_num_map: Dict[float, str] = {}
    slug_map: Dict[str, str] = {}

    sorted_seed = sorted(seed_chapters, key=_chapter_sort_key)

    for idx, seed in enumerate(sorted_seed, start=1):
        url = _normalize_url(seed["url"])
        print(f"[SEED {idx:04d}/{len(sorted_seed):04d}] {url}", flush=True)
        try:
            chap = fetch_chapter_content(url, story_url)
            if not chap.get("title") and seed.get("title"):
                chap["title"] = seed["title"]
            _register_chapter(chapters_by_url, chapter_num_map, slug_map, chap)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"⚠ Lỗi seed chapter {url}: {e}", flush=True)

    if not chapters_by_url:
        return []

    # Follow từ chapter lớn nhất hiện có
    chapters_sorted = sorted(chapters_by_url.values(), key=_chapter_sort_key)
    current = chapters_sorted[-1]
    current_url = _normalize_url(current["url"])
    visited_follow = set(chapters_by_url.keys())

    while current_url and len(chapters_by_url) < MAX_FOLLOW_CHAPTERS:
        current_chap = chapters_by_url.get(current_url)
        next_url = _normalize_url((current_chap or {}).get("next_url", ""))
        if not next_url:
            break
        if next_url in visited_follow:
            break
        if not _looks_like_chapter_url(next_url):
            break

        try:
            chap = fetch_chapter_content(next_url, story_url)
            added = _register_chapter(chapters_by_url, chapter_num_map, slug_map, chap)
            visited_follow.add(next_url)
            print(f"[FOLLOW] {'+' if added else '='} {next_url}", flush=True)
            current_url = _normalize_url(chap.get("url", next_url))
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"⚠ Lỗi follow next từ {current_url}: {e}", flush=True)
            break

    return sorted(chapters_by_url.values(), key=_chapter_sort_key)


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
    display_title = chap.get("title") or f"Chương {chapter_idx}"
    fname = f"{chapter_idx:04d} - {_safe_filename(display_title)}.html"
    path = os.path.join(out_dir, fname)
    html_out = HTML_TEMPLATE.format(
        doc_title=f"{book_title} - {display_title}",
        chapter_title=html.escape(display_title),
        book_title=html.escape(book_title or "Truyện"),
        src=chap.get("url") or "",
        content=chap.get("content_html") or "<p>(Không có nội dung)</p>",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_out)
    return path


def save_all_chapters_to_html(book_title: str, chapters: List[Dict[str, str]], out_dir: str) -> List[str]:
    saved: List[str] = []
    total = len(chapters)
    for i, chap in enumerate(chapters, start=1):
        try:
            p = save_chapter_html(book_title, i, chap, out_dir)
            print(f"[{i:04d}/{total:04d}] Saved HTML: {p}", flush=True)
            saved.append(p)
        except Exception as e:
            print(f"[{i:04d}/{total:04d}] ERROR save HTML {chap.get('url')}: {e}", flush=True)
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
    <meta name="cover" content="cover-image"/>
  </metadata>""" if cover_href else f"""
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{html.escape(book_title)}</dc:title>
    <dc:creator>{html.escape(author or '—')}</dc:creator>
    <dc:language>vi</dc:language>
    <dc:identifier id="BookId">{book_id}</dc:identifier>
    <dc:date>{dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</dc:date>
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

    print("\n[1/2] Lưu HTML...")
    html_paths = save_all_chapters_to_html(title, chapters, html_out_dir)
    if not html_paths:
        raise ValueError("Không lưu được HTML chương nào.")

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


if __name__ == "__main__":
    main()
