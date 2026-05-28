# -*- coding: utf-8 -*-
"""
Downloader cho https://metruyen.fit/ (Mê Truyện).

Trang info mẫu:
  https://metruyen.fit/truyen/nguoi-khac-bia/

Trang chương mẫu:
  https://metruyen.fit/truyen/nguoi-khac-bia/chuong-22/
"""

from __future__ import annotations

import html
import io
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from uuid import uuid4
from download_logger import chapter_log_line
from epub_metadata import PUBLISHER, subject_xml

try:
    import bs4
    import cloudscraper
    import requests
except ImportError:
    os.system(f'"{sys.executable}" -m pip install requests beautifulsoup4 lxml cloudscraper pillow')
    import bs4
    import requests
    try:
        import cloudscraper
    except ImportError:
        cloudscraper = None

from bs4 import BeautifulSoup

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    os.system(f'"{sys.executable}" -m pip install pillow')
    try:
        from PIL import Image
        HAS_PILLOW = True
    except ImportError:
        HAS_PILLOW = False


BASE_URL = "https://metruyen.fit/"
DEFAULT_URL = "https://metruyen.fit/truyen/nguoi-khac-bia/"
OUTPUT_BASE = Path("Output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    "Referer": BASE_URL,
}

TIMEOUT = 30
SLEEP_BETWEEN_CHAPS = 0.8
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)

_SCRAPER = None


def _safe_print(message: str = "") -> None:
    try:
        sys.stdout.buffer.write((message + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        print(message, flush=True)


def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""


def _clean_spaces(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", value).strip()


def _safe_filename(name: str, max_length: int = 150) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', " - ", name or "book")
    safe = re.sub(r"\s+", " ", safe).strip().strip(".")
    return (safe[:max_length].strip() or "book")


def _ensure_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return DEFAULT_URL
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url.lstrip("/")
    return url


def _absolute_url(page_url: str, href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return f"{urlparse(page_url).scheme or 'https'}:{href}"
    return urljoin(page_url, href)


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="", query="").geturl()


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_spaces(tag["content"])
    return ""


def _get_scraper():
    global _SCRAPER
    if _SCRAPER is not None:
        return _SCRAPER

    if cloudscraper is not None:
        _SCRAPER = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    else:
        _SCRAPER = requests.Session()
    _SCRAPER.headers.update(HEADERS)
    return _SCRAPER


def _http_get(url: str, referer: Optional[str] = None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    return _get_scraper().get(url, headers=headers, timeout=TIMEOUT)


def _detect_encoding(content: bytes, response=None) -> str:
    candidates: List[str] = []
    content_type = ""
    if response is not None:
        content_type = getattr(response, "headers", {}).get("content-type", "") or ""
    match = re.search(r"charset=([\w\-]+)", content_type, flags=re.I)
    if match:
        candidates.append(match.group(1))

    head = content[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset=['\"]?([\w\-]+)", head, flags=re.I)
    if match:
        candidates.append(match.group(1))

    apparent = getattr(response, "apparent_encoding", None) if response is not None else None
    if apparent:
        candidates.append(apparent)
    candidates.extend(["utf-8", "utf-8-sig"])

    for encoding in candidates:
        if not encoding:
            continue
        normalized = encoding.lower().replace("_", "-")
        if normalized in {"iso-8859-1", "latin-1", "ascii"}:
            continue
        try:
            content.decode(encoding)
            return encoding
        except Exception:
            continue
    return "utf-8"


def _decode_html(content: bytes, response=None) -> str:
    return content.decode(_detect_encoding(content, response), errors="replace")


def _make_soup(text: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(text, "lxml")
    except Exception:
        return BeautifulSoup(text, "html.parser")


class FetchHtmlError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _fetch_html_with_status(url: str, tries: int = 3, backoff: float = 0.8) -> Tuple[BeautifulSoup, int]:
    last_error: Optional[Exception] = None
    last_status: Optional[int] = None

    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(backoff * attempt)
        try:
            response = _http_get(url)
            status_code = getattr(response, "status_code", 200)
            last_status = status_code
            if status_code in RETRY_STATUS and attempt < tries:
                continue
            response.raise_for_status()
            content = getattr(response, "content", b"")
            if not content:
                text = getattr(response, "text", "")
                content = text.encode(getattr(response, "encoding", "utf-8") or "utf-8", errors="replace")
            return _make_soup(_decode_html(content, response)), status_code
        except Exception as exc:
            last_error = exc
            response = getattr(exc, "response", None)
            if response is not None and getattr(response, "status_code", None):
                last_status = getattr(response, "status_code")
            if attempt >= tries:
                break

    raise FetchHtmlError(f"Không tải được HTML: {url} ({last_error})", last_status)


def _fetch_html(url: str, tries: int = 3, backoff: float = 0.8) -> BeautifulSoup:
    soup, _ = _fetch_html_with_status(url, tries=tries, backoff=backoff)
    return soup


def _read_local_html(path: str | Path) -> BeautifulSoup:
    return _make_soup(_decode_html(Path(path).read_bytes()))


def _clean_title(title: str) -> str:
    title = _clean_spaces(title)
    title = re.sub(r"\s*•\s*Mê Truyện\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*-\s*Mê Truyện\s*$", "", title, flags=re.I)
    return title.strip() or "Unknown"


def _summary_item_value(soup: BeautifulSoup, label_pattern: str) -> str:
    pattern = re.compile(label_pattern, flags=re.I)
    for item in soup.select(".post-content_item"):
        heading = _clean_spaces(_text(item.select_one(".summary-heading h5")))
        if not pattern.search(heading):
            continue
        content = item.select_one(".summary-content")
        if not content:
            continue
        links = [_clean_spaces(_text(a)) for a in content.find_all("a") if _clean_spaces(_text(a))]
        if links:
            return ", ".join(dict.fromkeys(links))
        return _clean_spaces(content.get_text(" ", strip=True))
    return ""


def _extract_intro(soup: BeautifulSoup) -> str:
    node = soup.select_one(".description-summary .summary__content") or soup.select_one(".summary__content")
    if not node:
        return _meta_content(soup, "description", "og:description")

    clone = _make_soup(str(node))
    for bad in clone.find_all(["script", "style", "button"]):
        bad.decompose()
    for bad in clone.select(".c-content-readmore, .content-readmore"):
        bad.decompose()

    lines: List[str] = []
    p_nodes = clone.find_all("p")
    if p_nodes:
        for child in p_nodes:
            text = _clean_spaces(child.get_text(" ", strip=True))
            if text:
                lines.append(text)
    else:
        for child in clone.find_all(["div"], recursive=False):
            text = _clean_spaces(child.get_text(" ", strip=True))
            if text:
                lines.append(text)
    if not lines:
        text = clone.get_text("\n", strip=True)
        lines = [_clean_spaces(line) for line in text.splitlines() if _clean_spaces(line)]
    return "\n".join(lines).strip()


def _extract_declared_chapters(intro: str, page_text: str) -> int:
    for text in (intro, page_text):
        match = re.search(r"(?:Độ\s*dài|Số\s*chương)\s*[:：]?\s*(\d+)\s*chương", text, flags=re.I)
        if match:
            return int(match.group(1))
    return 0


def _get_book_info(soup: BeautifulSoup, page_url: str) -> Dict[str, str]:
    title = (
        _text(soup.select_one(".profile-manga .post-title h1"))
        or _text(soup.select_one(".post-title h1"))
        or _meta_content(soup, "og:image:alt")
        or _meta_content(soup, "og:title", "twitter:title")
        or _text(soup.find("title"))
    )
    title = _clean_title(title)

    author = _summary_item_value(soup, r"author|tác\s*giả")
    artist = _summary_item_value(soup, r"artist|nhóm\s*dịch")
    status = _summary_item_value(soup, r"status|trạng\s*thái")
    category = _summary_item_value(soup, r"genre|thể\s*loại")
    tags = _summary_item_value(soup, r"tag")
    release = _summary_item_value(soup, r"release|phát\s*hành")

    cover_url = _meta_content(soup, "og:image", "og:image:secure_url", "twitter:image")
    if not cover_url:
        img = soup.select_one(".summary_image img") or soup.select_one(".profile-manga img")
        if img:
            cover_url = (
                img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("src")
                or ""
            )
    if cover_url:
        cover_url = _absolute_url(page_url, cover_url)

    intro = _extract_intro(soup)
    page_text = _clean_spaces(soup.get_text(" ", strip=True))
    declared_chapters = _extract_declared_chapters(intro, page_text)

    return {
        "title": title,
        "author": author or "Unknown",
        "artist": artist,
        "status": status,
        "category": category,
        "tags": tags,
        "release": release,
        "cover_url": cover_url or "",
        "intro": intro,
        "declared_chapters": declared_chapters,
        "url": page_url,
    }


def _chapter_number(title: str, url: str = "") -> Optional[int]:
    for text in (url, title):
        match = re.search(r"(?:chuong|chương)[\-/\s]*(\d+)", text, flags=re.I)
        if match:
            return int(match.group(1))
    return None


def _book_base_path(page_url: str) -> str:
    path = urlparse(page_url).path
    match = re.search(r"(/truyen/[^/]+/)", path, flags=re.I)
    if match:
        return match.group(1)
    if "/chuong-" in path:
        return re.sub(r"chuong-\d+/?$", "", path, flags=re.I)
    return path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"


def _is_same_book_chapter(page_url: str, chapter_url: str) -> bool:
    page = urlparse(page_url)
    target = urlparse(chapter_url)
    if target.netloc and target.netloc != page.netloc:
        return False
    base_path = _book_base_path(page_url)
    return target.path.startswith(base_path) and re.search(r"/chuong-[^/]+/?$", target.path, flags=re.I) is not None


def _add_chapter(chapters: List[Dict[str, str]], seen: set[str], page_url: str, href: str, title: str) -> None:
    full_url = _normalize_url(_absolute_url(page_url, href))
    title = _clean_spaces(title) or "Chương"
    if not full_url or full_url in seen:
        return
    if not _is_same_book_chapter(page_url, full_url):
        return
    seen.add(full_url)
    chapters.append({"title": title, "url": full_url})


def _sort_chapters(chapters: List[Dict[str, str]]) -> List[Dict[str, str]]:
    numbered = [(_chapter_number(ch["title"], ch["url"]), ch) for ch in chapters]
    if numbered and all(num is not None for num, _ in numbered):
        return [ch for _, ch in sorted(numbered, key=lambda item: item[0] or 0)]
    return list(reversed(chapters))


def _get_list_chapters(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()

    containers = soup.select(".page-content-listing.single-page, .listing-chapters_wrap, ul.version-chap")
    for container in containers:
        for a in container.select("li.wp-manga-chapter a[href]"):
            _add_chapter(chapters, seen, page_url, a.get("href", ""), _text(a))

    if not chapters:
        for option in soup.select(".single-chapter-select option[data-redirect]"):
            _add_chapter(chapters, seen, page_url, option.get("data-redirect", ""), _text(option))

    if not chapters:
        for a in soup.select("li.wp-manga-chapter a[href]"):
            _add_chapter(chapters, seen, page_url, a.get("href", ""), _text(a))

    return _sort_chapters(chapters)


def _find_catalog_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> Optional[str]:
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical and canonical.get("href"):
        url = _absolute_url(chapter_url, canonical["href"])
        if "/chuong-" not in urlparse(url).path:
            return url

    for selector in ("a.btn.back[href]", ".btn-primary a[href]", ".breadcrumb a[href]"):
        for a in soup.select(selector):
            url = _absolute_url(chapter_url, a.get("href", ""))
            text = _clean_spaces(_text(a)).lower()
            if url and "/chuong-" not in urlparse(url).path and ("novel" in text or "info" in text or "/truyen/" in url):
                return url

    page = str(soup)
    for pattern in (
        r'"mangaUrl"\s*:\s*"([^"]+)"',
        r'"base_url"\s*:\s*"([^"]+)"',
    ):
        match = re.search(pattern, page)
        if match:
            return match.group(1).replace("\\/", "/")
    return None


def getText(url: str) -> Dict:
    url = _ensure_url(url)
    soup = _fetch_html(url)

    catalog_url = _find_catalog_url_from_chapter(soup, url)
    if catalog_url and _normalize_url(catalog_url) != _normalize_url(url):
        url = _normalize_url(catalog_url)
        soup = _fetch_html(url)

    chapters = _get_list_chapters(soup, url)
    info = _get_book_info(soup, url)
    total_chapters = len(chapters) or info.get("declared_chapters") or 0

    return {
        "title": info["title"],
        "author": info["author"],
        "artist": info.get("artist", ""),
        "status": info.get("status", ""),
        "category": info.get("category", ""),
        "tags": info.get("tags", ""),
        "release": info.get("release", ""),
        "chapters": chapters,
        "total_chapters": total_chapters,
        "declared_chapters": info.get("declared_chapters", 0),
        "cover_url": info["cover_url"],
        "intro": info["intro"],
        "url": url,
    }


def _chapter_title_from_page(soup: BeautifulSoup, book_title: str = "", fallback: str = "") -> str:
    title = (
        _text(soup.select_one("#chapter-heading"))
        or _text(soup.select_one(".breadcrumb li.active"))
        or _text(soup.find("h1"))
        or fallback
    )
    title = _clean_spaces(title)
    if book_title and title.lower().startswith(book_title.lower()):
        title = title[len(book_title):].strip(" -:：")
    title = _clean_title(title)
    return title or fallback or "Chương"


_CHAPTER_NOISE_RE = re.compile(
    r"(MỞ\s+ỨNG\s+DỤNG\s+SHOPEE|mở\s+khóa\s+toàn\s+bộ\s+chương|Metruyen\s+và\s+đội\s+ngũ\s+Editor|"
    r"text-chapter-toolbar|Prev|Next|Novel\s+Info|Manga\s+Info|Bình\s+luận|Comments\s+for\s+chapter|"
    r"Email\s+của\s+bạn|Trả\s+lời|wp-manga|chapter-toolbar)",
    flags=re.I,
)


def _is_noise_line(line: str, title: str = "") -> bool:
    line = _clean_spaces(line)
    if not line:
        return True
    if title and line == title:
        return True
    if _CHAPTER_NOISE_RE.search(line):
        return True
    if re.fullmatch(r"(Home|All Mangas|Trang chủ|Search|Sign in|Sign up)", line, flags=re.I):
        return True
    return False


def _paragraphs_from_node(node, title: str = "") -> List[str]:
    paragraphs: List[str] = []
    p_nodes = node.find_all("p")
    if p_nodes:
        source_nodes = p_nodes
    else:
        source_nodes = [node]

    for p in source_nodes:
        for br in p.find_all("br"):
            br.replace_with("\n")
        raw = p.get_text("\n", strip=False)
        for line in raw.splitlines():
            line = _clean_spaces(line)
            if _is_noise_line(line, title=title):
                continue
            paragraphs.append(line)
    return paragraphs


def _get_chapter_content_html(soup: BeautifulSoup, title: str = "") -> str:
    content = (
        soup.select_one(".read-container .reading-content .text-left")
        or soup.select_one(".read-container .reading-content")
        or soup.select_one(".entry-content_wrap .reading-content")
        or soup.select_one(".reading-content")
    )
    if not content:
        candidates = [
            node for node in soup.select(".entry-content, article, .content")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 200
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return "<p>(Không có nội dung)</p>"

    clone = _make_soup(str(content))
    root = clone.select_one(".text-left") or clone

    for bad in root.find_all(["script", "style", "input", "select", "button", "iframe", "ins", "form"]):
        bad.decompose()
    for bad in root.select(
        "#text-chapter-toolbar, .wp-manga-nav, .select-view, .select-pagination, "
        ".chapter-heading, .popup, .popup-content, .ndt-notice, .ads, .advertisement"
    ):
        bad.decompose()

    paragraphs = _paragraphs_from_node(root, title=title)
    if not paragraphs:
        return "<p>(Không có nội dung)</p>"
    return "\n".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)


def fetch_chapter_content(
    url: str,
    retries: int = 3,
    *,
    fallback_title: str = "",
    book_title: str = "",
) -> Dict:
    last_status: Optional[int] = None
    for attempt in range(1, retries + 1):
        try:
            soup, status_code = _fetch_html_with_status(url)
            last_status = status_code
            title = _chapter_title_from_page(soup, book_title=book_title, fallback=fallback_title)
            content_html = _get_chapter_content_html(soup, title=title)
            text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
            time.sleep(SLEEP_BETWEEN_CHAPS)
            return {
                "title": title,
                "content_html": content_html,
                "text": text,
                "url": url,
                "status_code": status_code,
            }
        except FetchHtmlError as exc:
            last_status = exc.status_code
            if attempt < retries:
                time.sleep(SLEEP_BETWEEN_CHAPS * attempt)
        except Exception:
            if attempt < retries:
                time.sleep(SLEEP_BETWEEN_CHAPS * attempt)

    return {
        "title": fallback_title or "Chương lỗi",
        "content_html": "<p>(Không tải được nội dung)</p>",
        "text": "(Không tải được nội dung)",
        "url": url,
        "status_code": last_status or "ERR",
    }


def _chapter_html_doc(title: str, content_html: str, source_url: str = "") -> str:
    source = f'<p class="source"><a href="{html.escape(source_url)}">{html.escape(source_url)}</a></p>' if source_url else ""
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; line-height: 1.75; max-width: 860px; margin: 2rem auto; padding: 0 1rem; }}
    h1 {{ font-size: 1.5rem; text-align: center; }}
    p {{ margin: 0.7em 0; }}
    .source {{ color: #666; font-size: 0.9rem; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <article class="chapter-content">
{content_html}
  </article>
  {source}
</body>
</html>
"""


def _read_cached_chapter(html_path: Path) -> Dict[str, str]:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    title = _text(soup.find("h1")) or _text(soup.find("title")) or html_path.stem
    article = soup.select_one("article.chapter-content") or soup.select_one("article") or soup.find("body") or soup
    for node in article.select(".source"):
        node.decompose()
    return {
        "title": title,
        "content_html": "\n".join(str(child) for child in article.contents).strip(),
        "url": str(html_path),
        "status_code": "CACHE",
    }


def _chapter_html_path(book_dir: Path, idx: int, title: str) -> Path:
    return book_dir / f"{idx:04d} - {_safe_filename(title, 90)}.html"


def _find_cached_chapter_path(book_dir: Path, idx: int) -> Optional[Path]:
    for pattern in (f"{idx:04d} - *.html", f"{idx:04d}.html", f"chapter_{idx:04d}.html"):
        matches = sorted(book_dir.glob(pattern))
        if matches:
            return matches[0]
    html_dir = book_dir / "html"
    if html_dir.is_dir():
        for pattern in (f"{idx:04d} - *.html", f"{idx:04d}.html", f"chapter_{idx:04d}.html"):
            matches = sorted(html_dir.glob(pattern))
            if matches:
                return matches[0]
    return None


def _normalize_range(total: int, start: int = 1, end: Optional[int] = None) -> Tuple[int, int]:
    if total <= 0:
        return 1, 0
    start = max(1, start)
    end = total if end is None else min(total, end)
    if end < start:
        raise ValueError("Khoảng chương không hợp lệ")
    return start, end


def _save_chapter_html(
    chapter: Dict[str, str],
    idx: int,
    book_dir: Path,
    *,
    book_title: str = "",
    force: bool = False,
) -> Dict[str, str]:
    book_dir.mkdir(parents=True, exist_ok=True)
    cached_path = _find_cached_chapter_path(book_dir, idx)
    if cached_path and not force:
        return _read_cached_chapter(cached_path)

    data = fetch_chapter_content(
        chapter["url"],
        fallback_title=chapter.get("title", f"Chương {idx}"),
        book_title=book_title,
    )
    title = data.get("title") or chapter.get("title") or f"Chương {idx}"
    html_path = _chapter_html_path(book_dir, idx, title)
    html_path.write_text(_chapter_html_doc(title, data["content_html"], data.get("url", chapter["url"])), encoding="utf-8")
    data["html_path"] = str(html_path)
    return data


def download_chapters(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
    force: bool = False,
) -> List[Dict[str, str]]:
    start, end = _normalize_range(len(chapters), start, end)
    total_selected = end - start + 1
    _safe_print(f"Bắt đầu tải/cache {total_selected} chương vào: {book_dir}")
    saved: List[Dict[str, str]] = []

    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        data = _save_chapter_html(
            chapter,
            idx,
            book_dir,
            book_title=book_info.get("title", ""),
            force=force,
        )
        saved.append(data)
        _safe_print(chapter_log_line(done, total_selected, data.get("status_code", "ERR"), idx, len(chapters), data.get("title") or chapter.get("title") or ""))

    _safe_print(f"Hoàn tất tải/cache {total_selected} chương.")
    return saved


def _download_cover(cover_url: str) -> Tuple[Optional[bytes], Optional[str]]:
    if not cover_url or not cover_url.startswith(("http://", "https://")):
        return None, None
    try:
        response = _http_get(cover_url)
        response.raise_for_status()
        content = response.content
        content_type = response.headers.get("content-type", "").lower()
        ext = Path(urlparse(cover_url).path).suffix.lower()
        if "png" in content_type:
            ext = ".png"
        elif "webp" in content_type:
            ext = ".webp"
        elif "gif" in content_type:
            ext = ".gif"
        elif ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            ext = ".jpg"
        return content, ext
    except Exception as exc:
        _safe_print(f"Không tải được cover: {exc}")
        return None, None


def _resize_cover(content: bytes, ext: str) -> Tuple[bytes, str]:
    if not content or not HAS_PILLOW:
        return content, ext
    try:
        image = Image.open(io.BytesIO(content))
        image.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
        if image.mode in ("RGBA", "LA"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1])
            image = background
        else:
            image = image.convert("RGB")

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90, optimize=True)
        return output.getvalue(), ".jpg"
    except Exception as exc:
        _safe_print(f"Không xử lý được cover bằng Pillow: {exc}")
        return content, ext


def _cover_media_type(ext: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get((ext or "").lower(), "image/jpeg")


def _load_cover(book_info: Dict[str, str], book_dir: Path) -> Tuple[Optional[bytes], Optional[str]]:
    cover_url = book_info.get("cover_url") or ""
    if not cover_url:
        _safe_print("[Cover] Không tìm thấy cover.")
        return None, None

    _safe_print(f"[Cover] Đang tải cover: {cover_url}")
    cover_bytes, cover_ext = _download_cover(cover_url)
    if cover_bytes and cover_ext:
        cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
        cover_path = book_dir / f"cover{cover_ext}"
        cover_path.write_bytes(cover_bytes)
        _safe_print(f"[Cover] Đã lưu cover: {cover_path}")
        return cover_bytes, cover_ext
    return None, None


def _save_book_info(book_info: Dict[str, str], chapters: List[Dict[str, str]], book_dir: Path) -> None:
    lines = [
        f"Title: {book_info.get('title', '')}",
        f"Author: {book_info.get('author', '')}",
        f"Artist: {book_info.get('artist', '')}",
        f"Status: {book_info.get('status', '')}",
        f"Category: {book_info.get('category', '')}",
        f"Tags: {book_info.get('tags', '')}",
        f"Release: {book_info.get('release', '')}",
        f"URL: {book_info.get('url', '')}",
        f"Cover: {book_info.get('cover_url', '')}",
        f"Chapters: {len(chapters)}",
    ]
    if book_info.get("declared_chapters"):
        lines.append(f"Declared chapters: {book_info.get('declared_chapters')}")
    lines.extend(["", book_info.get("intro", ""), "", "Mục lục:"])
    for idx, chapter in enumerate(chapters, 1):
        lines.append(f"{idx:04d}. {chapter['title']} - {chapter['url']}")
    (book_dir / "book_info.txt").write_text("\n".join(lines), encoding="utf-8")


def _zip_write(zf: zipfile.ZipFile, arcname: str, data, *, compress: bool = True) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    info = zipfile.ZipInfo(arcname)
    info.date_time = time.localtime(time.time())[:6]
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    zf.writestr(info, data)


def _xhtml_page(title: str, body_html: str, *, css_href: str = "../Styles/style.css") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi" lang="vi">
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


def _intro_html(book_info: Dict[str, str]) -> str:
    title = book_info.get("title") or "Truyện"
    author = book_info.get("author") or "Unknown"
    lines = [
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="meta"><strong>Tác giả:</strong> {html.escape(author)}</p>',
    ]
    if book_info.get("artist"):
        lines.append(f'<p class="meta"><strong>Nhóm dịch:</strong> {html.escape(book_info["artist"])}</p>')
    if book_info.get("status"):
        lines.append(f'<p class="meta"><strong>Trạng thái:</strong> {html.escape(book_info["status"])}</p>')
    if book_info.get("category"):
        lines.append(f'<p class="meta"><strong>Thể loại:</strong> {html.escape(book_info["category"])}</p>')
    intro = book_info.get("intro", "")
    if intro:
        lines.append("<hr/>")
        for line in intro.splitlines():
            line = _clean_spaces(line)
            if line:
                lines.append(f'<p class="intro">{html.escape(line)}</p>')
    return "\n".join(lines)


def build_epub_manual(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
    cover_bytes: Optional[bytes] = None,
    cover_ext: Optional[str] = None,
) -> Path:
    start, end = _normalize_range(len(chapters), start, end)
    items: List[Dict[str, str]] = []

    for idx in range(start, end + 1):
        html_path = _find_cached_chapter_path(book_dir, idx)
        if not html_path:
            _save_chapter_html(chapters[idx - 1], idx, book_dir, book_title=book_info.get("title", ""))
            html_path = _find_cached_chapter_path(book_dir, idx)
        if html_path:
            items.append(_read_cached_chapter(html_path))

    if not items:
        raise ValueError("Không có chương để tạo EPUB.")

    title = book_info.get("title") or "Truyện"
    author = book_info.get("author") or "Unknown"
    range_suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    epub_path = OUTPUT_BASE / f"{_safe_filename(title)} _ {_safe_filename(author)}{range_suffix}.epub"
    epub_path.parent.mkdir(parents=True, exist_ok=True)

    uid = f"urn:uuid:{uuid4()}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cover_ext = (cover_ext or ".jpg").lower()
    if cover_ext == ".jpeg":
        cover_ext = ".jpg"
    has_cover = bool(cover_bytes and cover_ext)
    cover_name = f"Images/cover{cover_ext if cover_ext in {'.jpg', '.png', '.webp', '.gif'} else '.jpg'}"
    cover_media = _cover_media_type(cover_ext)

    style_css = """
body { font-family: serif; line-height: 1.75; margin: 5%; }
h1 { font-size: 1.35em; line-height: 1.35; margin: 0 0 1em; text-align: center; }
p { margin: 0.65em 0; text-indent: 2em; }
.meta, .intro { text-indent: 0; }
.cover { text-align: center; margin: 0; text-indent: 0; }
.cover img { max-width: 100%; max-height: 95vh; height: auto; }
hr { border: 0; border-top: 1px solid #ddd; margin: 1em 0; }
""".strip()

    manifest_items = [
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="style" href="Styles/style.css" media-type="text/css"/>',
        '<item id="titlepage" href="Text/title.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_items = ['<itemref idref="titlepage"/>']
    nav_points = [
        '<navPoint id="nav-title" playOrder="1"><navLabel><text>Giới thiệu</text></navLabel>'
        '<content src="Text/title.xhtml"/></navPoint>'
    ]
    play_order = 2

    if has_cover:
        manifest_items.append(f'<item id="cover-image" href="{cover_name}" media-type="{cover_media}"/>')
        manifest_items.append('<item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
        spine_items.insert(0, '<itemref idref="cover"/>')
        nav_points.insert(
            0,
            '<navPoint id="nav-cover" playOrder="1"><navLabel><text>Cover</text></navLabel>'
            '<content src="Text/cover.xhtml"/></navPoint>',
        )
        nav_points[1] = nav_points[1].replace('playOrder="1"', 'playOrder="2"')
        play_order = 3

    for order, chapter in enumerate(items, 1):
        file_name = f"Text/chapter_{order:04d}.xhtml"
        manifest_items.append(f'<item id="chap{order}" href="{file_name}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="chap{order}"/>')
        nav_points.append(
            f'<navPoint id="nav{order}" playOrder="{play_order}">'
            f'<navLabel><text>{html.escape(chapter["title"])}</text></navLabel>'
            f'<content src="{file_name}"/></navPoint>'
        )
        play_order += 1

    metadata_cover = '<meta name="cover" content="cover-image"/>' if has_cover else ""
    guide_cover = '<guide><reference type="cover" title="Cover" href="Text/cover.xhtml"/></guide>' if has_cover else ""
    subjects = subject_xml(book_info.get("category") or book_info.get("tags"))
    content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:identifier id="BookId">{html.escape(uid)}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator opf:role="aut">{html.escape(author)}</dc:creator>
    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>
{subjects}    <dc:language>vi</dc:language>
    <dc:source>{html.escape(book_info.get("url", ""))}</dc:source>
    <dc:date>{datetime.now(timezone.utc).strftime("%Y-%m-%d")}</dc:date>
    <meta name="dcterms:modified" content="{modified}"/>
    {metadata_cover}
  </metadata>
  <manifest>
    {"".join(manifest_items)}
  </manifest>
  <spine toc="ncx">
    {"".join(spine_items)}
  </spine>
  {guide_cover}
</package>
"""

    toc_ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{html.escape(uid)}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(title)}</text></docTitle>
  <navMap>{"".join(nav_points)}</navMap>
</ncx>
"""

    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    _safe_print(f"[Epub] Đang đóng gói EPUB: {epub_path}")
    with zipfile.ZipFile(epub_path, "w") as zf:
        _zip_write(zf, "mimetype", "application/epub+zip", compress=False)
        _zip_write(zf, "META-INF/container.xml", container_xml)
        _zip_write(zf, "OEBPS/Styles/style.css", style_css)
        _zip_write(zf, "OEBPS/content.opf", content_opf)
        _zip_write(zf, "OEBPS/toc.ncx", toc_ncx)
        _zip_write(zf, "OEBPS/Text/title.xhtml", _xhtml_page(title, _intro_html(book_info)))

        if has_cover:
            _zip_write(zf, f"OEBPS/{cover_name}", cover_bytes)
            cover_body = f'<p class="cover"><img src="../{cover_name}" alt="{html.escape(title)}"/></p>'
            _zip_write(zf, "OEBPS/Text/cover.xhtml", _xhtml_page("Cover", cover_body))

        for order, chapter in enumerate(items, 1):
            body = f"<h1>{html.escape(chapter['title'])}</h1>\n{chapter.get('content_html') or '<p>(Không có nội dung)</p>'}"
            _zip_write(zf, f"OEBPS/Text/chapter_{order:04d}.xhtml", _xhtml_page(chapter["title"], body))

    _safe_print(f"[Epub] Đã tạo xong EPUB: {epub_path}")
    return epub_path


def _prepare_book_context(url: str) -> Tuple[Dict[str, str], List[Dict[str, str]], Path, Optional[bytes], Optional[str]]:
    url = _ensure_url(url)
    _safe_print("Đang lấy thông tin truyện...")
    data = getText(url)
    chapters = data.get("chapters", [])
    if not chapters:
        raise ValueError("Không tìm thấy danh sách chương.")

    book_info = {
        "title": data.get("title") or "Unknown",
        "author": data.get("author") or "Unknown",
        "artist": data.get("artist", ""),
        "status": data.get("status", ""),
        "category": data.get("category", ""),
        "tags": data.get("tags", ""),
        "release": data.get("release", ""),
        "intro": data.get("intro", ""),
        "cover_url": data.get("cover_url", ""),
        "url": data.get("url", url),
        "declared_chapters": data.get("declared_chapters", 0),
    }

    book_dir = OUTPUT_BASE / _safe_filename(book_info["title"])
    book_dir.mkdir(parents=True, exist_ok=True)
    _save_book_info(book_info, chapters, book_dir)
    epub_preview_path = OUTPUT_BASE / f"{_safe_filename(book_info['title'])} _ {_safe_filename(book_info['author'])}.epub"

    _safe_print("\n-----------------Thông tin truyện-----------------")
    _safe_print(f"Tên truyện     : {book_info['title']}")
    _safe_print(f"Tác giả        : {book_info['author']}")
    if book_info.get("artist"):
        _safe_print(f"Nhóm dịch      : {book_info['artist']}")
    if book_info.get("status"):
        _safe_print(f"Trạng thái     : {book_info['status']}")
    if book_info.get("category"):
        _safe_print(f"Thể loại       : {book_info['category']}")
    _safe_print(f"Số chương      : {len(chapters)}")
    if book_info.get("declared_chapters"):
        _safe_print(f"Độ dài khai báo : {book_info['declared_chapters']} chương")
    _safe_print(f"Cover          : {book_info.get('cover_url') or 'N/A'}")
    _safe_print(f"Thư mục HTML   : {book_dir}")
    _safe_print(f"EPUB sẽ lưu    : {epub_preview_path}")

    cover_bytes, cover_ext = _load_cover(book_info, book_dir)
    return book_info, chapters, book_dir, cover_bytes, cover_ext


def _ask_int(prompt: str, default: Optional[int] = None) -> int:
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        try:
            return int(raw)
        except ValueError:
            _safe_print("Vui lòng nhập số hợp lệ.")


def _print_download_menu() -> None:
    _safe_print("\n-----------------Menu-----------------")
    _safe_print("[1] Tải tất cả ( Html + Epub ) ( Mặc định )")
    _safe_print("[2] Tải từ X tới Y ( html )")
    _safe_print("[3] Tải chương X ( html )")
    _safe_print("[4] Thoát")


def _post_task_menu() -> bool:
    _safe_print("\n-----------------Menu-----------------")
    _safe_print("[1] Nhập Url truyện mới")
    _safe_print("[2] Thoát ( Mặc định )")
    choice = input("Chọn [2]: ").strip() or "2"
    return choice == "1"


def main() -> None:
    _safe_print("Downloader metruyen.fit / Mê Truyện")
    while True:
        raw_url = input("Nhập Url : ").strip()
        if not raw_url:
            _safe_print("URL trống, vui lòng nhập lại.")
            continue

        try:
            book_info, chapters, book_dir, cover_bytes, cover_ext = _prepare_book_context(raw_url)
        except Exception as exc:
            _safe_print(f"Lỗi: {exc}")
            continue

        while True:
            _print_download_menu()
            choice = input("Chọn [1]: ").strip() or "1"

            try:
                if choice == "1":
                    download_chapters(book_info, chapters, book_dir)
                    build_epub_manual(book_info, chapters, book_dir, cover_bytes=cover_bytes, cover_ext=cover_ext)
                    break
                if choice == "2":
                    start = _ask_int("Chương bắt đầu: ")
                    end = _ask_int("Chương kết thúc: ", len(chapters))
                    start, end = _normalize_range(len(chapters), start, end)
                    download_chapters(book_info, chapters, book_dir, start=start, end=end)
                    break
                if choice == "3":
                    idx = _ask_int("Chương cần tải: ")
                    idx, _ = _normalize_range(len(chapters), idx, idx)
                    download_chapters(book_info, chapters, book_dir, start=idx, end=idx)
                    break
                if choice == "4":
                    return
                _safe_print("Lựa chọn không hợp lệ.")
            except Exception as exc:
                _safe_print(f"Lỗi: {exc}")

        if not _post_task_menu():
            return



try:
    from download_policy import install_adapter_policy as _install_adapter_policy
    _install_adapter_policy(globals())
except Exception:
    pass
if __name__ == "__main__":
    import sys as _sys
    from adapter_cli import dispatch_or_menu as _dispatch_or_menu
    _dispatch_or_menu(_sys.modules[__name__], main, default_url=globals().get("DEFAULT_URL", ""))
