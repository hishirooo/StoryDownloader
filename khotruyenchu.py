# -*- coding: utf-8 -*-
"""
Downloader for https://khotruyenchu.space/.

Book sample:
  https://khotruyenchu.space/truyen/huyen-giam-tien-toc/

Chapter sample:
  https://khotruyenchu.space/chuong-73-phan-sat/
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import argparse
import html
import io
import re
import sys
import time

from download_logger import chapter_log_line

try:
    from curl_cffi import requests as http_requests

    USE_CURL_CFFI = True
except ImportError:
    import requests as http_requests

    USE_CURL_CFFI = False


BASE_URL = "https://khotruyenchu.space/"
DEFAULT_URL = "https://khotruyenchu.space/truyen/huyen-giam-tien-toc/"
OUTPUT_BASE = Path("output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8,zh;q=0.7",
    "Referer": BASE_URL,
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 25
SLEEP_BETWEEN_PAGES = 0.25
SLEEP_BETWEEN_CHAPS = 0.25
CHAPTER_RETRIES = 4
RETRY_STATUS = {403, 429, 500, 502, 503, 504}


class FetchHtmlError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


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
    value = html.unescape(value or "").replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def _safe_filename(name: str, max_length: int = 150) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', " - ", name or "book")
    safe = re.sub(r"\s+", " ", safe).strip().strip(".")
    return (safe[:max_length].strip() or "book")


def slugify_vi(value: str) -> str:
    return _safe_filename(value)


def _looks_like_local_file(value: str) -> bool:
    if not value:
        return False
    if re.match(r"^https?://", value, flags=re.I):
        return False
    return Path(value).exists()


def _ensure_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return DEFAULT_URL
    if _looks_like_local_file(url):
        return str(Path(url))
    if not re.match(r"^https?://", url, flags=re.I):
        return "https://" + url.lstrip("/")
    return url


def _absolute_url(page_url: str, href: str) -> str:
    href = html.unescape(href or "").strip()
    if not href:
        return page_url
    if href.startswith("//"):
        return "https:" + href
    if _looks_like_local_file(page_url) and re.match(r"^https?://", href, flags=re.I):
        return href
    if _looks_like_local_file(page_url):
        if href.startswith("./") or href.startswith("../"):
            return str((Path(page_url).parent / href).resolve())
        if not re.match(r"^[a-z][a-z0-9+.-]*:", href, flags=re.I):
            return str((Path(page_url).parent / href).resolve())
    base = page_url if urlparse(page_url).scheme else BASE_URL
    return urljoin(base, href)


def _normalized_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return str(Path(url))
    return parsed._replace(fragment="", query="").geturl().rstrip("/")


def _http_get(url: str, *, referer: Optional[str] = None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    kwargs = {"headers": headers, "timeout": TIMEOUT}
    if USE_CURL_CFFI:
        kwargs["impersonate"] = "chrome110"
    try:
        return http_requests.get(url, **kwargs)
    except TypeError as exc:
        if USE_CURL_CFFI and "impersonate" in str(exc).lower():
            kwargs.pop("impersonate", None)
            return http_requests.get(url, **kwargs)
        raise


def _detect_encoding(content: bytes, response=None) -> str:
    candidates: List[str] = []
    ctype = getattr(response, "headers", {}).get("content-type", "") if response is not None else ""
    match = re.search(r"charset=([\w\-]+)", ctype, flags=re.I)
    if match:
        candidates.append(match.group(1))
    head = content[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset=['\"]?([\w\-]+)", head, flags=re.I)
    if match:
        candidates.append(match.group(1))
    for attr in ("encoding", "apparent_encoding"):
        enc = getattr(response, attr, None) if response is not None else None
        if enc:
            candidates.append(enc)
    candidates.extend(["utf-8", "cp1258", "windows-1258"])
    for enc in candidates:
        try:
            content.decode(enc)
            return enc
        except Exception:
            pass
    return "utf-8"


def _decode_html(content: bytes, response=None) -> str:
    return content.decode(_detect_encoding(content, response), errors="replace")


def _fetch_html_with_status(url: str, tries: int = 3, *, referer: Optional[str] = None) -> Tuple[BeautifulSoup, object]:
    url = _ensure_url(url)
    if _looks_like_local_file(url):
        content = Path(url).read_bytes()
        return BeautifulSoup(_decode_html(content), "html.parser"), "FILE"

    last_status: Optional[int] = None
    last_error: Optional[Exception] = None
    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(0.8 * attempt)
        try:
            response = _http_get(url, referer=referer)
            status_code = getattr(response, "status_code", 200)
            last_status = status_code
            if status_code in RETRY_STATUS and attempt < tries:
                continue
            response.raise_for_status()
            return BeautifulSoup(_decode_html(response.content, response), "html.parser"), status_code
        except Exception as exc:
            last_error = exc
    raise FetchHtmlError(f"Khong tai duoc HTML: {url} ({last_error})", last_status)


def _fetch_html(url: str, tries: int = 3, *, referer: Optional[str] = None) -> BeautifulSoup:
    soup, _ = _fetch_html_with_status(url, tries=tries, referer=referer)
    return soup


def _canonical_url(soup: BeautifulSoup, page_url: str) -> str:
    canonical = soup.select_one("link[rel='canonical'][href]")
    if canonical and canonical.get("href"):
        return _absolute_url(page_url, canonical["href"])
    return page_url


def _archive_base_url(url: str) -> str:
    if _looks_like_local_file(url):
        return DEFAULT_URL
    parsed = urlparse(url)
    path = re.sub(r"/page/\d+/?$", "/", parsed.path.rstrip("/") + "/")
    return parsed._replace(path=path, query="", fragment="").geturl()


def _is_chapter_url(url: str) -> bool:
    return bool(re.search(r"/chuong-[^/]+/?$", urlparse(url).path, flags=re.I))


def _book_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> str:
    article = soup.find("article")
    if article:
        for cls in article.get("class", []):
            if cls.startswith("bo_truyen-"):
                return f"{BASE_URL.rstrip('/')}/truyen/{cls.replace('bo_truyen-', '')}/"
    for a in soup.select("a[href*='/truyen/']"):
        href = a.get("href", "")
        if "/truyen/" in href:
            return _archive_base_url(_absolute_url(chapter_url, href))
    return DEFAULT_URL


def _meta_value(hero: BeautifulSoup, label: str) -> str:
    if not hero:
        return ""
    for span in hero.select(".truyen-meta span"):
        text = _clean_spaces(span.get_text(" ", strip=True))
        if label.lower() in text.lower():
            strong = span.find("strong")
            value = _text(strong) or re.sub(rf".*?{re.escape(label)}\s*:?", "", text, flags=re.I).strip()
            return _clean_spaces(value)
    return ""


def _local_image_url(page_url: str, img) -> str:
    if not img:
        return ""
    if _looks_like_local_file(page_url):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if src.startswith("./") or src.startswith("../"):
            return str((Path(page_url).parent / src).resolve())
        if src and _looks_like_local_file(src):
            return str(Path(src).resolve())
    return (img.get("data-src") or img.get("src") or "").strip()


def _get_total_pages(soup: BeautifulSoup) -> int:
    nums: List[int] = []
    title = _text(soup.find("title"))
    match = re.search(r"Page\s+\d+\s+of\s+(\d+)", title, flags=re.I)
    if match:
        nums.append(int(match.group(1)))
    for node in soup.select(".page-numbers"):
        text = _clean_spaces(node.get_text(" ", strip=True))
        match = re.search(r"/\s*(\d+)$", text)
        if match:
            nums.append(int(match.group(1)))
        elif text.isdigit():
            nums.append(int(text))
        href = node.get("href") if hasattr(node, "get") else ""
        match = re.search(r"/page/(\d+)/", href or "")
        if match:
            nums.append(int(match.group(1)))
    for link in soup.select("link[rel='next'][href], link[rel='prev'][href], a[href*='/page/']"):
        match = re.search(r"/page/(\d+)/", link.get("href", ""))
        if match:
            nums.append(int(match.group(1)))
    return max(nums) if nums else 1


def _build_page_url(book_url: str, page: int) -> str:
    base = _archive_base_url(book_url).rstrip("/") + "/"
    if page <= 1:
        return base
    return f"{base}page/{page}/"


def _extract_book_info(soup: BeautifulSoup, page_url: str) -> Dict[str, object]:
    canonical = _canonical_url(soup, page_url)
    book_url = _archive_base_url(canonical)
    hero = soup.select_one(".truyen-hero-box")

    title = (
        _text(hero.select_one(".truyen-title")) if hero else ""
    ) or _text(soup.select_one("h1.truyen-title, h1.page-title, .hero-section h1"))
    title = re.sub(r"^Bộ truyện\s+", "", title, flags=re.I).strip()
    if not title:
        title = _text(soup.find("title"))
        title = re.sub(r"\s+Archives.*$", "", title, flags=re.I)
        title = re.sub(r"\s+-\s+Page\s+\d+\s+of\s+\d+.*$", "", title, flags=re.I)
        title = re.sub(r"\s+-\s+Tàng Kinh.*$", "", title, flags=re.I)

    author = _meta_value(hero, "Tác giả")
    genre = _meta_value(hero, "Thể loại")
    status = _meta_value(hero, "Tình trạng")

    intro_node = hero.select_one(".truyen-desc") if hero else None
    if not intro_node:
        intro_node = soup.select_one(".taxonomy-description")
    intro = _clean_spaces(intro_node.get_text("\n", strip=True) if intro_node else "")
    if not intro:
        meta = soup.select_one("meta[property='og:description'][content], meta[name='description'][content]")
        intro = _clean_spaces(meta.get("content", "") if meta else "")

    cover_url = ""
    img = hero.select_one(".truyen-cover img") if hero else None
    if not img:
        img = soup.select_one("img.wp-post-image, img[data-src*='cover'], img[src*='cover']")
    if img:
        cover_url = _local_image_url(page_url, img)
        if cover_url and not _looks_like_local_file(cover_url):
            cover_url = _absolute_url(page_url, cover_url)

    latest_node = soup.select_one(".btn-chuong-moi[href], .truyen-recent a[href]")
    latest_chapter = _text(latest_node) if latest_node else ""
    latest_chapter_url = _absolute_url(page_url, latest_node.get("href", "")) if latest_node else ""

    return {
        "title": _clean_spaces(title) or "Truyen",
        "author": _clean_spaces(author) or "Unknown",
        "category": genre,
        "genre": genre,
        "status": status,
        "intro": intro,
        "cover_url": cover_url,
        "url": book_url,
        "total_pages": _get_total_pages(soup),
        "latest_chapter": _clean_spaces(latest_chapter),
        "latest_chapter_url": latest_chapter_url,
    }


def _extract_chapters_from_page(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    selectors = [
        "article.entry-card h2.entry-title a[href]",
        ".entries article h2.entry-title a[href]",
        ".entries article h2 a[href]",
        "article h2.entry-title a[href]",
    ]
    anchors = []
    for selector in selectors:
        anchors = soup.select(selector)
        if anchors:
            break

    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    for a in anchors:
        title = _clean_spaces(a.get("title") or _text(a))
        url = _absolute_url(page_url, a.get("href", ""))
        if not title or not re.search(r"(Chương|/chuong-)", title + " " + url, flags=re.I):
            continue
        key = _normalized_url(url)
        if key in seen:
            continue
        seen.add(key)
        chapters.append({"title": title, "url": url})
    return chapters


def _fetch_all_chapters(book_info: Dict[str, object], first_soup: BeautifulSoup, *, local_input: bool = False) -> List[Dict[str, str]]:
    book_url = str(book_info.get("url") or DEFAULT_URL)
    total_pages = int(book_info.get("total_pages") or _get_total_pages(first_soup) or 1)
    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()

    def add_from(soup: BeautifulSoup, page_url: str) -> None:
        for chapter in _extract_chapters_from_page(soup, page_url):
            key = _normalized_url(chapter["url"])
            if key in seen:
                continue
            seen.add(key)
            chapters.append(chapter)

    add_from(first_soup, book_url)
    if local_input:
        return chapters

    for page in range(2, total_pages + 1):
        page_url = _build_page_url(book_url, page)
        try:
            soup = _fetch_html(page_url, referer=book_url)
        except Exception as exc:
            _safe_print(f"Canh bao: bo qua page muc luc {page}: {exc}")
            break
        add_from(soup, page_url)
        time.sleep(SLEEP_BETWEEN_PAGES)
    return chapters


def getText(url: str = DEFAULT_URL) -> Dict:
    url = _ensure_url(url)
    soup = _fetch_html(url)
    local_input = _looks_like_local_file(url)

    if not local_input and _is_chapter_url(url):
        book_url = _book_url_from_chapter(soup, url)
        soup = _fetch_html(book_url, referer=url)
        url = book_url

    info = _extract_book_info(soup, url)
    first_soup = soup
    if not local_input and _archive_base_url(_canonical_url(soup, url)) != str(info["url"]):
        first_soup = _fetch_html(str(info["url"]), referer=url)

    chapters = _fetch_all_chapters(info, first_soup, local_input=local_input)
    if chapters:
        info["latest_chapter"] = chapters[-1]["title"]
        info["latest_chapter_url"] = chapters[-1]["url"]
    info["total_chapters"] = len(chapters)
    return {**info, "chapters": chapters}


def _chapter_title_from_page(soup: BeautifulSoup, fallback: str = "") -> str:
    title = _text(soup.select_one("h1.page-title, article h1, h1.entry-title, h1"))
    if not title:
        title = _text(soup.find("title"))
        title = re.sub(r"^\s*Huyền Giám Tiên Tộc\s+-\s+", "", title, flags=re.I)
        title = re.sub(r"\s+-\s+Tàng Kinh.*$", "", title, flags=re.I)
    return _clean_spaces(title) or fallback or "Chapter"


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    trash = re.compile(
        r"(Chương trước|Chương sau|Mục lục|Cỡ chữ|Giao diện|Tìm kiếm truyện|Top xếp hạng|"
        r"Độc giả yêu cầu|Đăng k[ií] truyện|Comments?|Leave a Reply|Label \{\}|adsbygoogle|"
        r"khotruyenchu\.space|Tàng Kinh Các|The Converter|font size)",
        flags=re.I,
    )
    lines: List[str] = []
    for raw_line in raw_text.split("\n"):
        line = _clean_spaces(raw_line)
        if not line or len(line) < 2:
            continue
        if title and line == title:
            continue
        if trash.search(line):
            continue
        lines.append(line)
    return lines


def _extract_chapter_paragraphs(soup: BeautifulSoup, title: str = "") -> List[str]:
    content = soup.select_one(".entry-content")
    if not content:
        candidates = [
            node for node in soup.select("article, main, .content")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 500
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return []

    content = BeautifulSoup(str(content), "html.parser")
    for node in content.find_all(["script", "style", "noscript", "iframe", "ins", "select", "input", "button"]):
        node.decompose()
    for node in content.select(
        ".story-navigation, .reading-tools-bar, .code-block, .wpd-form, .wpdiscuz, "
        "#comments, .comments-area, .sharedaddy, .post-navigation, .adsbygoogle, .google-auto-placed"
    ):
        node.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")
    return _clean_chapter_lines(content.get_text("\n", strip=False), title=title)


def fetch_chapter_content(
    url: str,
    retries: int = CHAPTER_RETRIES,
    *,
    fallback_title: str = "",
    book_title: str = "",
) -> Dict:
    url = _ensure_url(url)
    last_status: Optional[int] = None
    for attempt in range(1, retries + 1):
        try:
            soup, status_code = _fetch_html_with_status(url, tries=1, referer=BASE_URL)
            last_status = status_code if isinstance(status_code, int) else None
            title = _chapter_title_from_page(soup, fallback=fallback_title)
            paragraphs = _extract_chapter_paragraphs(soup, title=title)
            if not paragraphs:
                raise FetchHtmlError("No chapter content", last_status)
            content_html = "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
            text = "\n".join(paragraphs)
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
        except Exception:
            pass
        if attempt < retries:
            time.sleep(0.8 * attempt)

    return {
        "title": fallback_title or "Chapter error",
        "content_html": "<p>(Khong tai duoc noi dung)</p>",
        "text": "(Khong tai duoc noi dung)",
        "url": url,
        "status_code": last_status or "ERR",
    }


def _chapter_html_doc(title: str, content_html: str, source_url: str = "") -> str:
    source = f'<p class="source"><a href="{html.escape(source_url)}">{html.escape(source_url)}</a></p>' if source_url else ""
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
</head>
<body>
  <article class="chapter">
    <h1>{html.escape(title)}</h1>
    {content_html}
    {source}
  </article>
</body>
</html>
"""


def _chapter_html_path(book_dir: str | Path, idx: int, title: str = "") -> Path:
    Path(book_dir).mkdir(parents=True, exist_ok=True)
    suffix = f" - {_safe_filename(title, 110)}" if title else ""
    return Path(book_dir) / f"{idx:04d}{suffix}.html"


def _find_cached_chapter_path(book_dir: str | Path, idx: int) -> Optional[Path]:
    root = Path(book_dir)
    matches = sorted(root.glob(f"{idx:04d}*.html")) if root.exists() else []
    if matches:
        return matches[0]
    html_dir = root / "html"
    matches = sorted(html_dir.glob(f"{idx:04d}*.html")) if html_dir.exists() else []
    return matches[0] if matches else None


def _looks_like_failed_content(text: str) -> bool:
    return not _clean_spaces(text) or bool(re.search(r"Khong tai duoc noi dung|Khong co noi dung|Chapter error", text, flags=re.I))


def _read_cached_chapter(html_path: Path) -> Dict[str, str]:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    title = _text(soup.find("h1")) or _text(soup.find("title")) or html_path.stem
    article = soup.select_one("article.chapter") or soup.find("body") or soup
    article = BeautifulSoup(str(article), "html.parser")
    for node in article.select(".source"):
        node.decompose()
    for h1 in article.find_all("h1"):
        h1.decompose()
    content_html = "\n".join(str(child) for child in article.contents).strip() or "<p>(Khong co noi dung)</p>"
    text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
    return {
        "title": title,
        "content_html": content_html,
        "text": text,
        "url": str(html_path),
        "status_code": "ERR_CACHE" if _looks_like_failed_content(text) else "CACHE",
        "html_path": str(html_path),
    }


def _is_failed_chapter_data(data: Dict[str, object]) -> bool:
    text = str(data.get("text") or BeautifulSoup(str(data.get("content_html") or ""), "html.parser").get_text("\n", strip=True))
    if _looks_like_failed_content(text):
        return True
    return data.get("status_code") not in ("CACHE", "FILE", 200)


def _write_chapter_html(data: Dict[str, str], book_dir: str | Path, idx: int, source_url: str = "") -> Path:
    html_path = _chapter_html_path(book_dir, idx, data.get("title") or f"Chuong {idx}")
    html_path.write_text(
        _chapter_html_doc(data.get("title") or f"Chuong {idx}", data.get("content_html") or "", data.get("url") or source_url),
        encoding="utf-8",
    )
    data["html_path"] = str(html_path)
    return html_path


def _save_chapter_html(chapter: Dict[str, str], idx: int, book_dir: str | Path, *, force: bool = False) -> Dict[str, str]:
    cached_path = _find_cached_chapter_path(book_dir, idx)
    if cached_path and not force:
        data = _read_cached_chapter(cached_path)
        if not _is_failed_chapter_data(data):
            return data
    data = fetch_chapter_content(chapter["url"], fallback_title=chapter.get("title", f"Chuong {idx}"))
    if not _is_failed_chapter_data(data):
        _write_chapter_html(data, book_dir, idx, chapter.get("url", ""))
    return data


def _normalize_range(total: int, start: int = 1, end: Optional[int] = None) -> Tuple[int, int]:
    if total <= 0:
        return 1, 0
    start = max(1, int(start or 1))
    end = total if end is None else min(total, int(end))
    if end < start:
        raise ValueError("Khoang chuong khong hop le")
    return start, end


def download_chapters(
    book_info: Dict[str, object],
    chapters: List[Dict[str, str]],
    book_dir: str | Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
    force: bool = False,
) -> List[Dict[str, str]]:
    start, end = _normalize_range(len(chapters), start, end)
    downloaded: List[Dict[str, str]] = []
    for idx in range(start, end + 1):
        data = _save_chapter_html(chapters[idx - 1], idx, book_dir, force=force)
        downloaded.append(data)
        _safe_print(chapter_log_line(idx - start + 1, end - start + 1, data.get("status_code", "ERR"), idx, len(chapters), data.get("title") or chapters[idx - 1].get("title") or ""))
    return downloaded


def save_all_chapters_to_html(title: str, chapters: List[Dict[str, str]], out_dir: str, start: int = 1, end=None) -> None:
    book_info = {"title": title}
    download_chapters(book_info, chapters, out_dir, start=start, end=end)


def _selected_chapter_data(
    book_info: Dict[str, object],
    chapters: List[Dict[str, str]],
    book_dir: str | Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
) -> List[Dict[str, str]]:
    start, end = _normalize_range(len(chapters), start, end)
    items: List[Dict[str, str]] = []
    for idx in range(start, end + 1):
        cached = _find_cached_chapter_path(book_dir, idx)
        if cached:
            data = _read_cached_chapter(cached)
            if _is_failed_chapter_data(data):
                data = _save_chapter_html(chapters[idx - 1], idx, book_dir)
        else:
            data = _save_chapter_html(chapters[idx - 1], idx, book_dir)
        items.append(data)
    return items


def build_epub(
    book_info: Dict[str, object],
    chapters: List[Dict[str, str]],
    book_dir: str | Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
    cover_bytes: Optional[bytes] = None,
    cover_ext: Optional[str] = None,
) -> Path:
    import epub_builder

    book_dir = Path(book_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    start, end = _normalize_range(len(chapters), start, end)
    selected = chapters[start - 1 : end]
    chapters_data = _selected_chapter_data(book_info, chapters, book_dir, start=start, end=end)
    suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    epub_path = book_dir / f"{_safe_filename(str(book_info.get('title', 'Truyen')))}{suffix}.epub"
    noise = io.StringIO()
    with redirect_stdout(noise):
        epub_builder.create_epub(
            book_url=str(book_info.get("url", "")),
            book_title=str(book_info.get("title", "Truyen")),
            author=str(book_info.get("author", "Unknown")),
            chapters=selected,
            fetch_fn=fetch_chapter_content,
            cover_bytes=cover_bytes,
            cover_ext=cover_ext or ".jpg",
            out_epub_path=str(epub_path),
            html_cache_dir=str(book_dir),
            chapters_data=chapters_data,
            language="vi",
            tags=book_info.get("category", ""),
            book_info=book_info,
        )
    _safe_print(f"[Epub] Da tao xong ebook: {epub_path}")
    return epub_path


def _download_cover(cover_url: str) -> Tuple[Optional[bytes], Optional[str]]:
    if not cover_url:
        return None, None
    if _looks_like_local_file(cover_url):
        path = Path(cover_url)
        return path.read_bytes(), path.suffix.lower() or ".jpg"
    try:
        response = _http_get(cover_url)
        response.raise_for_status()
        ext = Path(urlparse(cover_url).path).suffix.lower() or ".jpg"
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            ext = ".jpg"
        return response.content, ext
    except Exception:
        return None, None


def fetch_cover_from_book_page(book_page_url: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    soup = _fetch_html(book_page_url)
    info = _extract_book_info(soup, _ensure_url(book_page_url))
    content, ext = _download_cover(str(info.get("cover_url") or ""))
    return content, ext, str(info.get("cover_url") or "") if content else None


def _prepare_book_dir(book_info: Dict[str, object]) -> Path:
    return OUTPUT_BASE / _safe_filename(str(book_info.get("title") or "Truyen"))


def _run_once(args: argparse.Namespace) -> None:
    data = getText(args.url or DEFAULT_URL)
    book_dir = _prepare_book_dir(data)
    chapters = data.get("chapters", [])
    if not chapters:
        raise RuntimeError("Khong tim thay chuong")
    start, end = _normalize_range(len(chapters), args.start, args.end)
    download_chapters(data, chapters, book_dir, start=start, end=end, force=args.force)
    if not args.no_epub:
        build_epub(data, chapters, book_dir, start=start, end=end)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Download khotruyenchu.space chapters and build EPUB.")
    parser.add_argument("url", nargs="?", help=f"Book/chapter URL. Default: {DEFAULT_URL}")
    parser.add_argument("--start", type=int, default=1, help="Start chapter index")
    parser.add_argument("--end", type=int, default=None, help="End chapter index")
    parser.add_argument("--force", action="store_true", help="Refetch even when cache exists")
    parser.add_argument("--no-epub", action="store_true", help="Only download/cache HTML")
    args = parser.parse_args(argv)
    _run_once(args)


if __name__ == "__main__":
    main()
