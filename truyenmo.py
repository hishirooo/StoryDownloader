#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Downloader for truyenmo.com.

Features:
  1. Save chapters as HTML
  2. Save chapters as TXT
  3. Save HTML + TXT
  4. Save HTML + build EPUB
  5. Save TXT + build EPUB
  6. Save HTML + TXT + build EPUB

The script accepts either a story URL or a locally saved story info HTML page.
"""

from __future__ import annotations

import argparse
import html
import io
import os
import re
import sys
import time
import unicodedata
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from download_logger import chapter_log_line

import requests
from bs4 import BeautifulSoup, NavigableString
from bs4.element import Doctype
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import epub_builder

    HAS_EPUB_BUILDER = True
except Exception:
    HAS_EPUB_BUILDER = False


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


BASE_SITE = "https://truyenmo.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
}
TIMEOUT = 25
SLEEP_BETWEEN_CHAPTERS = 0.18
OUTPUT_DIR = "output"
AD_URL_MARKERS = (
    "s.shopee.vn",
    "shopee.vn",
    "lazada.vn",
    "vt.tiktok.com",
    "tiktok.com",
    "click-here-to-unlock",
)
AD_TEXT_MARKERS = (
    "mời quý độc giả",
    "moi quy doc gia",
    "click vào liên kết",
    "click vao lien ket",
    "mở ứng dụng shopee",
    "mo ung dung shopee",
    "tiếp tục đọc toàn bộ chương",
    "tiep tuc doc toan bo chuong",
    "truyện mơ và đội ngũ editor xin chân thành cảm ơn",
    "truyen mo va doi ngu editor xin chan thanh cam on",
)


def safe_print(message: str = "") -> None:
    try:
        sys.stdout.buffer.write((message + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        print(message, flush=True)


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


SESSION = build_session()


def is_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def resolve_local_source(source: str) -> str:
    if os.path.exists(source):
        return source
    output_path = os.path.join(OUTPUT_DIR, source)
    if os.path.exists(output_path):
        return output_path
    return source


def read_local_html(path: str) -> str:
    data = Path(path).read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1258", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def fetch_text(url: str) -> Tuple[str, str]:
    response = SESSION.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() in {"iso-8859-1", "latin-1"}:
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text, response.url


def soup_from_source(source: str) -> Tuple[BeautifulSoup, str, bool]:
    source = resolve_local_source(source)
    if is_url(source):
        raw, final_url = fetch_text(source)
        soup = BeautifulSoup(raw, "html.parser")
        apply_css_before_text(soup)
        return soup, final_url, False

    raw = read_local_html(source)
    soup = BeautifulSoup(raw, "html.parser")
    apply_css_before_text(soup)
    base_url = infer_story_url(raw, soup) or Path(source).resolve().as_uri()
    return soup, base_url, True


def infer_story_url(raw_html: str, soup: BeautifulSoup) -> str:
    for selector in (
        'link[rel="canonical"][href]',
        'meta[property="og:url"][content]',
    ):
        tag = soup.select_one(selector)
        if tag:
            value = tag.get("href") or tag.get("content")
            if value and is_url(value):
                return value

    match = re.search(r"saved from url=\(\d+\)(https?://[^\s>]+)", raw_html, flags=re.I)
    if match:
        return match.group(1)
    return ""


def css_unescape(value: str) -> str:
    def repl_hex(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except Exception:
            return match.group(0)

    value = re.sub(r"\\([0-9a-fA-F]{1,6})\s?", repl_hex, value)
    value = value.replace(r"\A", "\n")
    value = value.replace(r"\"", '"').replace(r"\'", "'").replace(r"\\", "\\")
    return html.unescape(value)


def extract_css_before_map(soup: BeautifulSoup) -> Dict[str, str]:
    css_text = "\n".join(style.get_text("\n", strip=False) for style in soup.find_all("style"))
    rule_re = re.compile(
        r"\.([A-Za-z0-9_-]+):before\s*\{\s*content\s*:\s*(['\"])(.*?)\2\s*;?\s*\}",
        flags=re.S,
    )
    return {match.group(1): css_unescape(match.group(3)) for match in rule_re.finditer(css_text)}


def apply_css_before_text(soup: BeautifulSoup) -> None:
    before_map = extract_css_before_map(soup)
    if not before_map:
        return

    for tag in list(soup.select("[class]")):
        classes = tag.get("class") or []
        replacement = next((before_map[cls] for cls in classes if cls in before_map), None)
        if replacement is not None and not tag.get_text(strip=True):
            tag.replace_with(NavigableString(replacement))


def text_of(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def clean_spaces(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.sub(r"\s+([,.;:!?…])", r"\1", value)
    value = re.sub(r"([\(“‘])\s+", r"\1", value)
    value = re.sub(r"\s+([)”’])", r"\1", value)
    return value


def normalize_title(value: Optional[str]) -> str:
    title = clean_spaces(value or "")
    match = re.match(r"^(Chương)\s*(\d+)(\s*[:\-–]\s*)?(.*)$", title, flags=re.I)
    if match:
        prefix, number, _sep, rest = match.groups()
        rest = clean_spaces(rest)
        return f"{prefix} {number}: {rest}" if rest else f"{prefix} {number}"
    return title


def safe_filename(value: str, max_len: int = 150) -> str:
    value = (value or "chapter").replace("Đ", "D").replace("đ", "d")
    value = unicodedata.normalize("NFKD", value or "chapter")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r'[\\/:*?"<>|]+', " - ", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    return (value[:max_len] or "chapter").strip()


def slugify(value: str, max_len: int = 90) -> str:
    value = (value or "book").replace("Đ", "D").replace("đ", "d")
    value = unicodedata.normalize("NFKD", value or "book")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^\w\s-]+", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", "-", value).strip("-_")
    return (value[:max_len] or "book")


def get_meta_content(soup: BeautifulSoup, selector: str) -> str:
    tag = soup.select_one(selector)
    return text_of(tag) or (tag.get("content", "").strip() if tag else "")


def dd_after_dt(soup: BeautifulSoup, label_pattern: str) -> str:
    pattern = re.compile(label_pattern, flags=re.I)
    for dt in soup.find_all("dt"):
        if pattern.search(text_of(dt)):
            dd = dt.find_next_sibling("dd")
            return clean_spaces(text_of(dd))
    return ""


def get_book_info(soup: BeautifulSoup, story_url: str) -> Dict[str, object]:
    title = (
        text_of(soup.select_one('h2[itemprop="name"]'))
        or get_meta_content(soup, 'meta[property="og:title"]')
        or (soup.title.get_text(" ", strip=True).split(" - Truyện Mơ")[0] if soup.title else "")
    )
    author = text_of(soup.select_one('a[href*="/tac-gia/"]')) or get_meta_content(
        soup, 'meta[name="author"]'
    )
    status = dd_after_dt(soup, r"Trạng\s*thái") or dd_after_dt(soup, r"Tráº¡ng\s*th")
    team = text_of(soup.select_one('a[href*="/nhom-dich/"]'))

    genres = []
    for tag in soup.select('a[itemprop="genre"], a.cate-item'):
        genre = clean_spaces(text_of(tag))
        if genre and genre not in genres:
            genres.append(genre)

    cover_url = ""
    cover_meta = soup.select_one('meta[property="og:image"][content]')
    if cover_meta and cover_meta.get("content"):
        cover_url = urljoin(story_url, cover_meta["content"])
    if not cover_url:
        img = soup.select_one('img[itemprop="image"], .card img[src], img[src*="/images/story/"]')
        if img and img.get("src"):
            cover_url = urljoin(story_url, img["src"])

    description_node = soup.select_one(".story-description .inner, [itemprop='description']")
    description = ""
    if description_node:
        paragraphs = [clean_spaces(p.get_text(" ", strip=True)) for p in description_node.find_all("p")]
        description = "\n".join(p for p in paragraphs if p)

    return {
        "title": clean_spaces(title) or "TruyenMo",
        "author": clean_spaces(author) or "Unknown",
        "status": clean_spaces(status),
        "team": clean_spaces(team),
        "genres": genres,
        "cover_url": cover_url,
        "description": description,
    }


def chapter_sort_key(item: Dict[str, str]) -> Tuple[int, str]:
    text = f"{item.get('title', '')} {item.get('url', '')}"
    match = re.search(r"(?:chuong|chương)[-/\s]*(\d+)", text, flags=re.I)
    if match:
        return int(match.group(1)), item.get("url", "")
    return 10**9, item.get("url", "")


def get_chapter_list(soup: BeautifulSoup, story_url: str) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    seen = set()

    selectors = [
        ".list-chapters .episode-title a[href]",
        "#listChapters .episode-title a[href]",
        ".list-chapters a[href]",
    ]
    for selector in selectors:
        for link in soup.select(selector):
            href = link.get("href", "").strip()
            title = normalize_title(text_of(link))
            if not href or not title:
                continue
            full_url = urljoin(story_url or BASE_SITE, href)
            if "/chuong-" not in full_url.lower():
                continue
            if full_url in seen:
                continue
            seen.add(full_url)
            chapters.append({"title": title, "url": full_url})
        if chapters:
            break

    chapters.sort(key=chapter_sort_key)
    return chapters


def pick_chapter_title(soup: BeautifulSoup) -> str:
    hidden = soup.select_one('input#chapter_title[value], input[name="chapter_title"][value]')
    if hidden and hidden.get("value"):
        return normalize_title(hidden["value"])

    for selector in ("h1", ".chapter-title", ".card-title"):
        tag = soup.select_one(selector)
        title = normalize_title(text_of(tag))
        if title:
            title = re.sub(r"^.+?\s+-\s+(Chương\s+\d+.*)$", r"\1", title, flags=re.I)
            title = re.sub(r"\s+-\s+Truyện\s+Mơ$", "", title, flags=re.I)
            return normalize_title(title)

    if soup.title:
        title = soup.title.get_text(" ", strip=True)
        title = re.sub(r"^.+?\s+-\s+(Chương\s+\d+.*)$", r"\1", title, flags=re.I)
        title = re.sub(r"\s+-\s+Truyện\s+Mơ$", "", title, flags=re.I)
        return normalize_title(title)

    return ""


def pick_content_node(soup: BeautifulSoup):
    for selector in (
        "#chapter-content-render",
        ".chapter-content .content-container",
        ".chapter-content",
        "article",
    ):
        node = soup.select_one(selector)
        if node and text_of(node):
            return node
    return soup.body or soup


def absolutize_media(root, page_url: str) -> None:
    for tag in root.find_all(["a", "img", "source"]):
        attr = "href" if tag.name == "a" else "src"
        value = tag.get(attr)
        if value:
            tag[attr] = urljoin(page_url, value)


def is_ad_url(value: str) -> bool:
    value = (value or "").lower()
    return any(marker in value for marker in AD_URL_MARKERS)

def tag_contains_ad_url(tag) -> bool:
    for node in [tag, *tag.find_all(True)]:
        if is_ad_url(node.get("href", "")) or is_ad_url(node.get("src", "")):
            return True
    return False

def tag_contains_ad_text(tag) -> bool:
    text = clean_spaces(tag.get_text(" ", strip=True)).lower()
    return any(marker in text for marker in AD_TEXT_MARKERS)

def is_ad_node(tag) -> bool:
    return tag_contains_ad_url(tag) or tag_contains_ad_text(tag)

def strip_unwanted(root) -> None:
    for item in list(root.find_all(string=lambda value: isinstance(value, Doctype))):
        item.extract()

    remove_selectors = [
        "script",
        "style",
        "noscript",
        "iframe",
        "form",
        "input",
        "select",
        "button",
        ".ads",
        ".adsbygoogle",
        ".banner",
        ".my-4",
        ".chapter-nav",
        ".breadcrumb",
        ".social",
        ".comment",
        ".comments",
        ".fb-comments",
        ".d-flex.justify-content-center",
    ]
    for selector in remove_selectors:
        for tag in list(root.select(selector)):
            tag.decompose()

    for tag in list(root.select("a.btn, .btn")):
        tag.decompose()

    for tag in list(root.find_all(["div", "section", "aside"])):
        if is_ad_node(tag):
            tag.decompose()

    for tag in list(root.find_all(["p", "a", "img", "h3", "h4", "h5"])):
        if is_ad_node(tag):
            tag.decompose()

    for paragraph in list(root.find_all("p")):
        paragraph_text = clean_spaces(paragraph.get_text(" ", strip=True))
        low = paragraph_text.lower()
        if "truyenmo.com" in low or "truyện được đăng tải duy nhất" in low:
            paragraph.decompose()
        elif not paragraph_text and not paragraph.find("img"):
            paragraph.decompose()

    for tag in list(root.find_all(["div", "section", "aside"])):
        if not clean_spaces(tag.get_text(" ", strip=True)) and not tag.find("img"):
            tag.decompose()


def clean_attrs(root) -> None:
    for tag in root.find_all(True):
        keep = {}
        if tag.name == "a" and tag.get("href"):
            keep["href"] = tag["href"]
        elif tag.name == "img" and tag.get("src"):
            keep["src"] = tag["src"]
            if tag.get("alt"):
                keep["alt"] = tag["alt"]
        tag.attrs = keep


def inner_html(root) -> str:
    parts = []
    for child in root.children:
        if isinstance(child, Doctype):
            continue
        if isinstance(child, NavigableString) and not child.strip():
            continue
        parts.append(str(child))
    return "\n".join(parts).strip()


def clean_chapter_content(soup: BeautifulSoup, page_url: str) -> str:
    root = pick_content_node(soup)
    strip_unwanted(root)
    absolutize_media(root, page_url)
    clean_attrs(root)
    content = inner_html(root)
    if not content:
        text = clean_spaces(root.get_text(" ", strip=True))
        content = f"<p>{html.escape(text)}</p>" if text else "<p>(Không có nội dung)</p>"
    return content


def fetch_chapter(url: str) -> Dict[str, str]:
    response = SESSION.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() in {"iso-8859-1", "latin-1"}:
        response.encoding = response.apparent_encoding or "utf-8"

    raw = response.text
    final_url = response.url
    soup = BeautifulSoup(raw, "html.parser")
    apply_css_before_text(soup)
    title = pick_chapter_title(soup)
    content_html = clean_chapter_content(soup, final_url)
    return {
        "title": title,
        "content_html": content_html,
        "url": final_url,
        "status_code": response.status_code,
    }

def _normalize_range(total: int, start: int = 1, end: Optional[int] = None) -> Tuple[int, int]:
    if total < 1:
        raise ValueError("Không có chương để xử lý.")
    start = int(start or 1)
    end = total if end is None else int(end)
    start = max(1, start)
    end = min(total, end)
    if start > end:
        raise ValueError("Khoảng chương không hợp lệ.")
    return start, end

def _find_cached_chapter_path(out_dir: str | Path, idx: int) -> Optional[Path]:
    matches = sorted(Path(out_dir).glob(f"{idx:04d}*.html"))
    return matches[0] if matches else None

def _read_cached_chapter(path: str | Path) -> Dict[str, str]:
    path = Path(path)
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    article = soup.select_one("article") or soup.body or soup
    title = normalize_title(text_of(soup.find("h1"))) or path.stem
    source_link = soup.select_one(".meta a[href]") or soup.find("a", href=True)
    return {
        "title": title,
        "content_html": inner_html(article),
        "url": source_link.get("href", "") if source_link else "",
        "status_code": "CACHE",
    }

def _load_or_fetch_chapter(
    idx: int,
    chapter: Dict[str, str],
    out_dir: str | Path,
    book_title: str,
    *,
    save_html: bool,
    force: bool = False,
) -> Tuple[Dict[str, str], Optional[Path]]:
    if not force:
        cached_path = _find_cached_chapter_path(out_dir, idx)
        if cached_path:
            data = _read_cached_chapter(cached_path)
            if not data.get("url"):
                data["url"] = chapter.get("url", "")
            return data, cached_path

    data = fetch_chapter(chapter["url"])
    if not data.get("title"):
        data["title"] = chapter.get("title") or f"Chương {idx}"

    html_path = None
    if save_html:
        html_path = Path(save_chapter_html(book_title, idx, data, str(out_dir)))
    return data, html_path


HTML_TEMPLATE = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{doc_title}</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.75;max-width:860px;margin:2rem auto;padding:0 1rem;background:#f6f6f7;color:#202124}}
    h1{{font-size:1.6rem;margin:0 0 1rem}}
    .meta{{color:#666;font-size:.9rem;margin-bottom:1rem}}
    article{{background:#fff;border-radius:8px;padding:1.2rem 1.35rem;box-shadow:0 1px 8px rgba(0,0,0,.06)}}
    p{{margin:.65rem 0}}
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
</html>
"""


def save_chapter_html(book_title: str, idx: int, chapter: Dict[str, str], out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    title = chapter.get("title") or f"Chương {idx}"
    path = os.path.join(out_dir, f"{idx:04d} - {safe_filename(title)}.html")
    doc = HTML_TEMPLATE.format(
        doc_title=html.escape(f"{book_title} - {title}"),
        chapter_title=html.escape(title),
        book_title=html.escape(book_title),
        src=html.escape(chapter.get("url", "")),
        content=chapter.get("content_html") or "<p>(Không có nội dung)</p>",
    )
    with open(path, "w", encoding="utf-8") as file:
        file.write(doc)
    return path


def chapter_html_to_text(content_html: str) -> str:
    soup = BeautifulSoup(content_html or "", "html.parser")
    paragraphs = []
    for p in soup.find_all("p"):
        line = clean_spaces(p.get_text(" ", strip=True))
        if line:
            paragraphs.append(line)
    if not paragraphs:
        text = clean_spaces(soup.get_text(" ", strip=True))
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def save_chapter_txt(idx: int, chapter: Dict[str, str], out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    title = chapter.get("title") or f"Chương {idx}"
    path = os.path.join(out_dir, f"{idx:04d} - {safe_filename(title)}.txt")
    with open(path, "w", encoding="utf-8") as file:
        file.write(chapter_html_to_text(chapter.get("content_html", "")))
    return path


def iter_selected_chapters(
    chapters: List[Dict[str, str]], start: int = 1, end: Optional[int] = None
) -> Iterable[Tuple[int, Dict[str, str]]]:
    total = len(chapters)
    if not end or end > total:
        end = total
    start = max(1, start)
    for idx in range(start, end + 1):
        yield idx, chapters[idx - 1]


def download_chapters(
    book_title: str,
    chapters: List[Dict[str, str]],
    out_dir: str,
    mode: str,
    start: int = 1,
    end: Optional[int] = None,
    *,
    force: bool = False,
) -> Tuple[List[str], List[str], List[Dict[str, str]]]:
    html_paths: List[str] = []
    txt_paths: List[str] = []
    chapters_data: List[Dict[str, str]] = []
    save_html = mode in {"1", "3", "4", "6"}
    save_txt = mode in {"2", "3", "5", "6"}

    start, end = _normalize_range(len(chapters), start, end)
    selected = list(iter_selected_chapters(chapters, start, end))
    selected_count = len(selected)
    safe_print(f"Bắt đầu tải/cache {selected_count} chương vào: {out_dir}")

    for done, (idx, item) in enumerate(selected, 1):
        try:
            chapter, html_path = _load_or_fetch_chapter(
                idx,
                item,
                out_dir,
                book_title,
                save_html=save_html,
                force=force,
            )
            chapters_data.append(chapter)

            if save_html and html_path:
                html_paths.append(str(html_path))
            if save_txt:
                path = save_chapter_txt(idx, chapter, out_dir)
                txt_paths.append(path)

            title = chapter.get("title") or item.get("title") or f"Chương {idx}"
            safe_print(chapter_log_line(done, selected_count, chapter.get("status_code", "ERR"), idx, len(chapters), title))
            time.sleep(SLEEP_BETWEEN_CHAPTERS)
        except Exception as exc:
            title = item.get("title") or f"Chương {idx}"
            safe_print(chapter_log_line(done, selected_count, "ERR", idx, len(chapters), f"{title} ({exc})"))
            chapters_data.append(
                {
                    "title": title,
                    "content_html": f"<p>Không tải được chương này: {html.escape(str(exc))}</p>",
                    "url": item.get("url", ""),
                    "status_code": "ERR",
                }
            )

    safe_print(f"Hoàn tất tải/cache {selected_count} chương.")
    return html_paths, txt_paths, chapters_data


def download_cover(cover_url: str) -> Tuple[Optional[bytes], str]:
    if not cover_url:
        return None, ".jpg"
    try:
        response = SESSION.get(cover_url, timeout=TIMEOUT)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        path_ext = os.path.splitext(urlparse(cover_url).path)[1].lower()
        if "png" in content_type or path_ext == ".png":
            ext = ".png"
        else:
            ext = ".jpg"
        return response.content, ext
    except Exception as exc:
        safe_print(f"Không tải được cover: {exc}")
        return None, ".jpg"


def write_book_info(info: Dict[str, object], chapters: List[Dict[str, str]], story_url: str, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "book_info.txt")
    lines = [
        f"Title: {info.get('title') or ''}",
        f"Author: {info.get('author') or ''}",
        f"Status: {info.get('status') or ''}",
        f"Team: {info.get('team') or ''}",
        f"Genres: {', '.join(info.get('genres') or [])}",
        f"Total Chapters: {len(chapters)}",
        f"Source: {story_url}",
        "",
        str(info.get("description") or ""),
    ]
    with open(path, "w", encoding="utf-8") as file:
        file.write("\n".join(lines).strip() + "\n")
    return path


def build_epub_from_data(
    story_url: str,
    info: Dict[str, object],
    chapters: List[Dict[str, str]],
    chapters_data: List[Dict[str, str]],
    start: int,
    end: Optional[int],
    out_dir: str,
) -> Optional[str]:
    if not HAS_EPUB_BUILDER:
        safe_print("Không tìm thấy epub_builder.py, bỏ qua EPUB.")
        return None

    start, end = _normalize_range(len(chapters), start, end)
    selected_chapters = [item for _idx, item in iter_selected_chapters(chapters, start, end)]
    if not selected_chapters:
        safe_print("Không có chương để đóng EPUB.")
        return None

    cover_bytes, cover_ext = download_cover(str(info.get("cover_url") or ""))
    is_partial = start != 1 or end != len(chapters)
    suffix = f"-{start:04d}-{end:04d}" if is_partial else ""
    epub_path = os.path.join(OUTPUT_DIR, f"{slugify(str(info.get('title') or 'TruyenMo'))}{suffix}.epub")
    safe_print(f"[Epub] Đang tạo ebook: {epub_path}")
    noise = io.StringIO()
    with redirect_stdout(noise):
        created_path = epub_builder.create_epub(
            story_url,
            str(info.get("title") or "TruyenMo"),
            str(info.get("author") or "Unknown"),
            selected_chapters,
            fetch_fn=None,
            cover_bytes=cover_bytes,
            cover_ext=cover_ext,
            language="vi",
            creator="Hishiro",
            sleep=SLEEP_BETWEEN_CHAPTERS,
            out_epub_path=epub_path,
            html_cache_dir=out_dir,
            chapters_data=chapters_data,
            tags=info.get("genres") or [],
            book_info=info,
        )
    safe_print(f"[Epub] Đã tạo xong ebook: {created_path}")
    return created_path

def _selected_chapter_data(
    book_title: str,
    chapters: List[Dict[str, str]],
    out_dir: str | Path,
    start: int = 1,
    end: Optional[int] = None,
) -> List[Dict[str, str]]:
    start, end = _normalize_range(len(chapters), start, end)
    items: List[Dict[str, str]] = []
    for idx in range(start, end + 1):
        data, _html_path = _load_or_fetch_chapter(
            idx,
            chapters[idx - 1],
            out_dir,
            book_title,
            save_html=True,
        )
        items.append(data)
    return items


def clean_saved_html_file(path: str) -> bool:
    raw = read_local_html(path)
    soup = BeautifulSoup(raw, "html.parser")
    target = soup.select_one("article") or soup.select_one("#chapter-content-render")
    if not target:
        return False

    before = str(target)
    strip_unwanted(target)
    clean_attrs(target)
    after = str(target)
    if after == before:
        return False

    with open(path, "w", encoding="utf-8") as file:
        file.write(str(soup))
    return True

def clean_output_dir(path: str) -> Tuple[int, int]:
    folder = resolve_local_source(path)
    if not os.path.isdir(folder):
        raise SystemExit(f"Không tìm thấy thư mục: {path}")

    total = 0
    changed = 0
    for html_path in sorted(Path(folder).glob("*.html")):
        total += 1
        try:
            if clean_saved_html_file(str(html_path)):
                changed += 1
                safe_print(f"CLEAN: {html_path}")
        except Exception as exc:
            safe_print(f"ERROR {html_path}: {exc}")
    return total, changed

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tải truyện từ truyenmo.com")
    parser.add_argument("source", nargs="?", help="URL truyện hoặc file HTML info đã lưu")
    parser.add_argument("--mode", choices=("1", "2", "3", "4", "5", "6"), help="Chế độ tải")
    parser.add_argument("--start", type=int, help="Chương bắt đầu, mặc định 1")
    parser.add_argument("--end", type=int, help="Chương kết thúc, mặc định là chương cuối")
    parser.add_argument("--clean-dir", help="Dọn rác quảng cáo trong thư mục HTML đã tải sẵn")
    return parser.parse_args()


def prompt_if_needed(args: argparse.Namespace) -> argparse.Namespace:
    if not args.source:
        args.source = input("Nhập URL truyện hoặc file HTML info: ").strip()
    if not args.source:
        raise SystemExit("Thiếu URL hoặc file HTML info.")

    if not args.mode:
        safe_print("\n-----------------Menu-----------------")
        safe_print("[1] Tải và lưu HTML")
        safe_print("[2] Tải và lưu TXT")
        safe_print("[3] Tải và lưu HTML + TXT")
        safe_print("[4] Tải và lưu HTML + EPUB - Default")
        safe_print("[5] Tải và lưu TXT + EPUB")
        safe_print("[6] Tải và lưu HTML + TXT + EPUB")
        args.mode = input("Chọn [4]: ").strip() or "4"
    if args.mode not in {"1", "2", "3", "4", "5", "6"}:
        raise SystemExit("Lựa chọn không hợp lệ.")

    interactive = sys.stdin.isatty()

    if args.start is None and interactive:
        start_in = input("Chương bắt đầu [1]: ").strip()
        args.start = int(start_in) if start_in else 1
    elif args.start is None:
        args.start = 1

    if args.end is None and interactive:
        end_in = input("Chương kết thúc [tất cả]: ").strip()
        if end_in:
            args.end = int(end_in)
    return args

def _epub_preview_path(info: Dict[str, object]) -> str:
    return os.path.join(OUTPUT_DIR, f"{slugify(str(info.get('title') or 'TruyenMo'))}.epub")

def _print_book_info(
    source: str,
    story_url: str,
    is_local: bool,
    info: Dict[str, object],
    chapters: List[Dict[str, str]],
    out_dir: str,
    info_path: str,
) -> None:
    safe_print("\n-----------------Thông tin truyện-----------------")
    safe_print(f"Tên truyện     : {info.get('title')}")
    safe_print(f"Tác giả        : {info.get('author')}")
    if info.get("status"):
        safe_print(f"Trạng thái     : {info.get('status')}")
    if info.get("team"):
        safe_print(f"Nhóm dịch      : {info.get('team')}")
    genres = ", ".join(info.get("genres") or [])
    if genres:
        safe_print(f"Thể loại       : {genres}")
    safe_print(f"Số chương      : {len(chapters)}")
    safe_print(f"Nguồn          : {'local HTML' if is_local else 'web'}")
    safe_print(f"URL            : {story_url}")
    safe_print(f"Thư mục truyện : {out_dir}")
    safe_print(f"Book info      : {info_path}")
    safe_print(f"EPUB sẽ lưu    : {_epub_preview_path(info)}")
    description = str(info.get("description") or "")
    if description:
        safe_print(f"Giới thiệu     : {description[:160]}{'...' if len(description) > 160 else ''}")

def _load_book_context(source: str) -> Tuple[str, str, bool, Dict[str, object], List[Dict[str, str]], str]:
    source = resolve_local_source(source)
    safe_print("Đang lấy thông tin truyện...")
    soup, story_url, is_local = soup_from_source(source)
    info = get_book_info(soup, story_url)
    chapters = get_chapter_list(soup, story_url)

    if not chapters:
        raise SystemExit("Không tìm thấy danh sách chương trong trang info.")

    out_dir = os.path.join(OUTPUT_DIR, slugify(str(info.get("title") or "TruyenMo")))
    os.makedirs(out_dir, exist_ok=True)
    info_path = write_book_info(info, chapters, story_url, out_dir)
    _print_book_info(source, story_url, is_local, info, chapters, out_dir, info_path)
    return source, story_url, is_local, info, chapters, out_dir

def _print_done_summary(
    html_paths: List[str],
    txt_paths: List[str],
    epub_path: Optional[str],
    out_dir: str,
) -> None:
    safe_print("\n-------------------Hoàn tất-------------------")
    if html_paths:
        safe_print(f"HTML: {len(html_paths)} file tại {out_dir}")
    if txt_paths:
        safe_print(f"TXT : {len(txt_paths)} file tại {out_dir}")
    if epub_path:
        safe_print(f"EPUB: {epub_path}")

def _run_download_mode(
    story_url: str,
    info: Dict[str, object],
    chapters: List[Dict[str, str]],
    out_dir: str,
    mode: str,
    *,
    start: int = 1,
    end: Optional[int] = None,
) -> Tuple[List[str], List[str], List[Dict[str, str]], Optional[str]]:
    html_paths, txt_paths, chapters_data = download_chapters(
        str(info.get("title") or "TruyenMo"),
        chapters,
        out_dir,
        mode,
        start=start,
        end=end,
    )

    epub_path = None
    if mode in {"4", "5", "6"}:
        epub_path = build_epub_from_data(
            story_url,
            info,
            chapters,
            chapters_data,
            start=start,
            end=end,
            out_dir=out_dir,
        )

    _print_done_summary(html_paths, txt_paths, epub_path, out_dir)
    return html_paths, txt_paths, chapters_data, epub_path

def _build_epub_from_cache(
    story_url: str,
    info: Dict[str, object],
    chapters: List[Dict[str, str]],
    out_dir: str,
    *,
    start: int = 1,
    end: Optional[int] = None,
) -> Optional[str]:
    chapters_data = _selected_chapter_data(str(info.get("title") or "TruyenMo"), chapters, out_dir, start, end)
    epub_path = build_epub_from_data(story_url, info, chapters, chapters_data, start, end, out_dir)
    _print_done_summary([], [], epub_path, out_dir)
    return epub_path

def _ask_int(prompt: str, default: Optional[int] = None) -> int:
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        try:
            return int(raw)
        except ValueError:
            safe_print("Vui lòng nhập số hợp lệ.")

def _print_download_menu() -> None:
    safe_print("\n-----------------Menu-----------------")
    safe_print("[1] Tải tất cả ( Html + Epub ) ( Mặc định )")
    safe_print("[2] Tải từ X tới Y ( html )")
    safe_print("[3] Tải chương X ( html )")
    safe_print("[4] Thoát")


def _post_task_menu() -> bool:
    safe_print("\n-----------------Menu-----------------")
    safe_print("[1] Nhập Url truyện mới")
    safe_print("[2] Thoát ( Mặc định )")
    choice = input("Chọn [2]: ").strip() or "2"
    return choice == "1"


def interactive_main() -> None:
    safe_print("Downloader truyenmo.com / Truyện Mơ")
    while True:
        raw_source = input("Nhập Url truyện hoặc file HTML info: ").strip()
        if not raw_source:
            safe_print("URL trống, vui lòng nhập lại.")
            continue

        try:
            _source, story_url, _is_local, info, chapters, out_dir = _load_book_context(raw_source)
        except Exception as exc:
            safe_print(f"Lỗi: {exc}")
            continue

        while True:
            _print_download_menu()
            choice = input("Chọn [1]: ").strip() or "1"

            try:
                if choice == "1":
                    _run_download_mode(story_url, info, chapters, out_dir, "4")
                    break
                if choice == "2":
                    start = _ask_int("Chương bắt đầu: ")
                    end = _ask_int("Chương kết thúc: ", len(chapters))
                    start, end = _normalize_range(len(chapters), start, end)
                    _run_download_mode(story_url, info, chapters, out_dir, "1", start=start, end=end)
                    break
                if choice == "3":
                    idx = _ask_int("Chương cần tải: ")
                    idx, _ = _normalize_range(len(chapters), idx, idx)
                    _run_download_mode(story_url, info, chapters, out_dir, "1", start=idx, end=idx)
                    break
                if choice == "4":
                    return
                safe_print("Lựa chọn không hợp lệ.")
            except Exception as exc:
                safe_print(f"Lỗi: {exc}")

        if not _post_task_menu():
            return

def _should_use_interactive_menu(args: argparse.Namespace) -> bool:
    return (
        sys.stdin.isatty()
        and not args.source
        and not args.mode
        and args.start is None
        and args.end is None
        and not args.clean_dir
    )

def main() -> None:
    args = parse_args()
    if args.clean_dir:
        total, changed = clean_output_dir(args.clean_dir)
        safe_print(f"Hoàn tất dọn {changed}/{total} file HTML.")
        return

    if _should_use_interactive_menu(args):
        interactive_main()
        return

    args = prompt_if_needed(args)
    _source, story_url, _is_local, info, chapters, out_dir = _load_book_context(args.source)
    _run_download_mode(story_url, info, chapters, out_dir, args.mode, start=args.start, end=args.end)


if __name__ == "__main__":
    main()
