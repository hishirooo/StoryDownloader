# -*- coding: utf-8 -*-
"""Downloader adapter for https://www.bqxs.net/."""

from __future__ import annotations

from bs4 import BeautifulSoup
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import argparse
import base64
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

try:
    from PIL import Image

    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


BASE_URL = "https://www.bqxs.net/"
DEFAULT_URL = "https://www.bqxs.net/book/szdds/"
OUTPUT_BASE = Path("output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,vi;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": BASE_URL,
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 25
SLEEP_BETWEEN_PAGES = 0.5
SLEEP_BETWEEN_CHAPS = 0.7
CHAPTER_RETRIES = 4
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)
MAX_CATALOG_PAGES = 300
MAX_CHAPTER_PARTS = 30


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
    value = html.unescape(value or "")
    value = value.replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()


def _strip_label(value: str, *labels: str) -> str:
    value = _clean_spaces(value)
    for label in labels:
        value = re.sub(rf"^{re.escape(label)}\s*[:\uff1a]?\s*", "", value).strip()
    return value


def _safe_filename(name: str, max_length: int = 150) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', " - ", name or "book")
    safe = re.sub(r"\s+", " ", safe).strip().strip(".")
    return (safe[:max_length].strip() or "book")


def slugify_vi(value: str) -> str:
    return _safe_filename(value)


def _looks_like_local_file(value: str) -> bool:
    if not value:
        return False
    if re.match(r"^[a-z][a-z0-9+.-]*://", value, flags=re.I):
        return False
    try:
        return Path(value).exists()
    except OSError:
        return False


def _ensure_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return DEFAULT_URL
    if _looks_like_local_file(url):
        return str(Path(url))
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url.lstrip("/")
    return url


def _absolute_url(page_url: str, href: str) -> str:
    href = html.unescape(href or "").strip()
    if not href or href.startswith("javascript:"):
        return ""
    if href.startswith("//"):
        return f"{urlparse(page_url).scheme or 'https'}:{href}"
    if _looks_like_local_file(page_url):
        if re.match(r"^https?://", href, flags=re.I):
            return href
        if href.startswith("./") or href.startswith("../"):
            try:
                return str((Path(page_url).parent / href).resolve())
            except OSError:
                return href
        return urljoin(BASE_URL, href)
    base = page_url if urlparse(page_url).scheme else BASE_URL
    return urljoin(base, href)


def _normalized_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return str(Path(url))
    return parsed._replace(fragment="", query="").geturl().rstrip("/")


def _http_referer(value: str) -> str:
    return value if urlparse(value or "").scheme in {"http", "https"} else BASE_URL


def _http_get(url: str, *, referer: Optional[str] = None, stream: bool = False):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    kwargs = {"headers": headers, "timeout": TIMEOUT}
    if stream:
        kwargs["stream"] = True
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
    content_type = getattr(response, "headers", {}).get("content-type", "") if response is not None else ""
    match = re.search(r"charset=([\w\-]+)", content_type, flags=re.I)
    if match:
        candidates.append(match.group(1))

    head = content[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset=['\"]?([\w\-]+)", head, flags=re.I)
    if match:
        candidates.append(match.group(1))

    for attr in ("encoding", "apparent_encoding"):
        encoding = getattr(response, attr, None) if response is not None else None
        if encoding:
            candidates.append(encoding)

    candidates.extend(["utf-8", "gb18030", "gbk", "big5"])
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


def _fetch_html_with_status(
    url: str,
    tries: int = 3,
    backoff: float = 0.8,
    *,
    referer: Optional[str] = None,
) -> Tuple[BeautifulSoup, object]:
    url = _ensure_url(url)
    if _looks_like_local_file(url):
        content = Path(url).read_bytes()
        return BeautifulSoup(_decode_html(content), "html.parser"), "FILE"

    last_error: Optional[Exception] = None
    last_status: Optional[int] = None
    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(backoff * attempt)
        try:
            response = _http_get(url, referer=referer)
            status_code = getattr(response, "status_code", 200)
            last_status = status_code
            if status_code in RETRY_STATUS and attempt < tries:
                continue
            response.raise_for_status()
            content = getattr(response, "content", b"")
            if not content:
                text = getattr(response, "text", "")
                content = text.encode(getattr(response, "encoding", "utf-8") or "utf-8", errors="replace")
            return BeautifulSoup(_decode_html(content, response), "html.parser"), status_code
        except Exception as exc:
            last_error = exc
            response = getattr(exc, "response", None)
            if response is not None and getattr(response, "status_code", None):
                last_status = getattr(response, "status_code")
            if attempt >= tries:
                break

    raise FetchHtmlError(f"Khong tai duoc HTML: {url} ({last_error})", last_status)


def _fetch_html(url: str, tries: int = 3, backoff: float = 0.8, *, referer: Optional[str] = None) -> BeautifulSoup:
    soup, _ = _fetch_html_with_status(url, tries=tries, backoff=backoff, referer=referer)
    return soup


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_spaces(tag["content"])
    return ""


def _field_text(soup: BeautifulSoup, *labels: str) -> str:
    for node in soup.select("#info p, #maininfo p, .info p, .bookinfo p"):
        text = _clean_spaces(node.get_text(" ", strip=True))
        for label in labels:
            if text.startswith(label):
                return _strip_label(text, label)
    return ""


def _book_dir_from_url(url: str) -> str:
    path = urlparse(url if urlparse(url).scheme else DEFAULT_URL).path
    match = re.search(r"(?P<book_dir>/book/[^/]+/)", path, flags=re.I)
    return match.group("book_dir") if match else "/book/szdds/"


def _book_url_from_any(url: str) -> str:
    parsed = urlparse(url if urlparse(url).scheme else BASE_URL)
    book_dir = _book_dir_from_url(url)
    return parsed._replace(path=book_dir, query="", fragment="").geturl()


def _catalog_url_from_book_url(book_url: str, page_no: int = 1) -> str:
    parsed = urlparse(book_url if urlparse(book_url).scheme else DEFAULT_URL)
    book_dir = _book_dir_from_url(book_url)
    return parsed._replace(path=f"{book_dir}mulu_{page_no}.html", query="", fragment="").geturl()


def _is_book_url(url: str) -> bool:
    return bool(re.fullmatch(r"/book/[^/]+/", urlparse(url).path, flags=re.I))


def _is_catalog_url(url: str) -> bool:
    return bool(re.fullmatch(r"/book/[^/]+/mulu_\d+\.html?", urlparse(url).path, flags=re.I))


def _is_chapter_url(page_url: str, chapter_url: str, *, allow_part: bool = False) -> bool:
    page = urlparse(page_url if urlparse(page_url).scheme else DEFAULT_URL)
    target = urlparse(chapter_url)
    if target.netloc and page.netloc and target.netloc != page.netloc:
        return False
    book_dir = _book_dir_from_url(page_url if urlparse(page_url).scheme else DEFAULT_URL)
    if not target.path.startswith(book_dir):
        return False
    basename = target.path.rsplit("/", 1)[-1]
    pattern = r"[a-z0-9]+(?:_\d+)?\.html?" if allow_part else r"[a-z0-9]+\.html?"
    return re.fullmatch(pattern, basename, flags=re.I) is not None


def _chapter_base_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"_\d+(\.html?)$", r"\1", parsed.path, flags=re.I)
    return parsed._replace(path=path, query="", fragment="").geturl()


def _chapter_id_from_url(url: str) -> str:
    match = re.search(r"/([a-z0-9]+)(?:_\d+)?\.html?$", urlparse(url).path, flags=re.I)
    return match.group(1).lower() if match else ""


def _chapter_part_number(url: str) -> int:
    match = re.search(r"_(\d+)\.html?$", urlparse(url).path, flags=re.I)
    return int(match.group(1)) + 1 if match else 1


def _chapter_part_url(url: str, part_no: int) -> str:
    base = _chapter_base_url(url)
    if part_no <= 1:
        return base
    parsed = urlparse(base)
    path = re.sub(r"(\.html?)$", f"_{part_no - 1}\\1", parsed.path, flags=re.I)
    return parsed._replace(path=path, query="", fragment="").geturl()


def _catalog_page_number(url: str) -> int:
    match = re.search(r"mulu_(\d+)\.html?$", urlparse(url).path, flags=re.I)
    return int(match.group(1)) if match else 1


def _chapter_number(title: str) -> int:
    match = re.search(r"\u7b2c\s*(\d+)\s*\u7ae0", title or "")
    return int(match.group(1)) if match else 0


def _get_book_info(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, str]:
    title = (
        _meta_content(soup, "og:novel:book_name", "og:title")
        or _text(soup.select_one("#info h1"))
        or _text(soup.select_one("#maininfo h1"))
        or _text(soup.find("h1"))
    )
    if not title:
        title_tag = _text(soup.find("title"))
        title = re.split(r"_|-|\u7b14\u8da3\u5c0f\u8bf4|\u6700\u65b0\u7ae0\u8282", title_tag, maxsplit=1)[0].strip()

    author = _meta_content(soup, "og:novel:author") or _field_text(soup, "\u4f5c\u8005")
    category = _meta_content(soup, "og:novel:category") or _field_text(soup, "\u7c7b\u522b")
    status = _meta_content(soup, "og:novel:status") or _field_text(soup, "\u72b6\u6001")
    update_time = _meta_content(soup, "og:novel:update_time") or _field_text(soup, "\u6700\u540e\u66f4\u65b0")

    latest_chapter = (
        _meta_content(soup, "og:novel:latest_chapter_name", "og:novel:lastest_chapter_name")
        or _text(soup.select_one("#info a[rel='chapter']"))
        or _text(soup.select_one(".lastchapter a[rel='chapter']"))
    )
    latest_url = _meta_content(soup, "og:novel:latest_chapter_url", "og:novel:lastest_chapter_url")
    latest_node = soup.select_one("#info a[rel='chapter'][href], .lastchapter a[rel='chapter'][href]")
    if not latest_url and latest_node:
        latest_url = latest_node.get("href", "")
    if latest_url:
        latest_url = _absolute_url(page_url, latest_url)

    cover_url = _meta_content(soup, "og:image")
    if not cover_url:
        img = soup.select_one("#fmimg img[data-original], #fmimg img[src], .book-img img[src], img.lazy[data-original]")
        if img:
            cover_url = (img.get("data-original") or img.get("src") or "").strip()
    if cover_url:
        cover_url = _absolute_url(page_url, cover_url)

    intro = _meta_content(soup, "og:description", "description")
    intro_node = soup.select_one("#intro, .intro, .bookintro")
    if intro_node:
        intro = intro_node.get_text("\n", strip=True)

    book_url = _meta_content(soup, "og:novel:read_url", "og:url", "og:novel:url")
    canonical = soup.select_one("link[rel='canonical'][href]")
    if not book_url and canonical:
        book_url = canonical.get("href", "")
    if not book_url:
        book_url = _book_url_from_any(page_url)
    book_url = _absolute_url(page_url, book_url)

    total_chapters = 0
    for option in soup.select("select.form-control option[value], select#indexselect option[value]"):
        text = _clean_spaces(option.get_text(" ", strip=True))
        match = re.search(r"-\s*(\d+)\s*\u7ae0", text)
        if match:
            total_chapters = max(total_chapters, int(match.group(1)))
    nums = [_chapter_number(a.get_text(" ", strip=True)) for a in soup.select("#list a[rel='chapter'], #list a[href]")]
    nums = [num for num in nums if num > 0]
    if nums:
        total_chapters = max(total_chapters, max(nums))

    return {
        "title": _clean_spaces(title) or "Unknown",
        "author": _clean_spaces(author) or "Unknown",
        "status": _clean_spaces(status),
        "category": _clean_spaces(category),
        "genre": _clean_spaces(category),
        "update_time": _clean_spaces(update_time),
        "latest_chapter": _clean_spaces(latest_chapter),
        "latest_chapter_url": latest_url or "",
        "cover_url": cover_url or "",
        "intro": _clean_spaces(intro),
        "total_chapters": total_chapters,
        "url": book_url or page_url,
    }


def _find_catalog_url(soup: BeautifulSoup, page_url: str) -> str:
    if _is_catalog_url(page_url):
        return page_url
    for selector in (
        "a.chapterlist[href]",
        "a[href*='mulu_'][href]",
        "#info_url[href]",
        "a[rel='index'][href]",
    ):
        for a in soup.select(selector):
            href = _absolute_url(page_url, a.get("href", ""))
            if href and _is_catalog_url(href):
                return href
    return _catalog_url_from_book_url(_book_url_from_any(page_url), 1)


def _get_catalog_urls(soup: BeautifulSoup, page_url: str) -> List[str]:
    urls: List[str] = []
    if _is_catalog_url(page_url):
        urls.append(page_url)
    for option in soup.select("select.form-control option[value], select#indexselect option[value]"):
        href = _absolute_url(page_url, option.get("value", ""))
        if href and _is_catalog_url(href):
            urls.append(href)
    for a in soup.select(".index-container-btn[href], .index-container a[href], a[href*='mulu_'][href]"):
        href = _absolute_url(page_url, a.get("href", ""))
        if href and _is_catalog_url(href):
            urls.append(href)

    seen: set[str] = set()
    result: List[str] = []
    for url in urls:
        key = _normalized_url(url)
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
    return sorted(result, key=_catalog_page_number)


def _next_catalog_url(soup: BeautifulSoup, current_url: str) -> str:
    current_no = _catalog_page_number(current_url)
    for a in soup.select(".index-container-btn[href], a[href]"):
        text = _clean_spaces(_text(a))
        href = _absolute_url(current_url, a.get("href", ""))
        if not href or not _is_catalog_url(href):
            continue
        if _catalog_page_number(href) > current_no and ("\u4e0b\u4e00\u9875" in text or text.lower() == "next"):
            return href
    return ""


def _expand_catalog_urls(soup: BeautifulSoup, catalog_url: str) -> List[str]:
    initial = _get_catalog_urls(soup, catalog_url)
    if len(initial) > 1:
        return initial
    if not initial and _is_catalog_url(catalog_url):
        initial = [catalog_url]

    expanded: List[str] = []
    seen: set[str] = set()
    pending = list(initial)
    current_soup_by_url = {_normalized_url(catalog_url): soup}

    while pending and len(expanded) < MAX_CATALOG_PAGES:
        current_url = pending.pop(0)
        key = _normalized_url(current_url)
        if key in seen:
            continue
        seen.add(key)
        expanded.append(current_url)

        try:
            current_soup = current_soup_by_url.get(key) or _fetch_html(current_url, referer=catalog_url)
        except Exception as exc:
            _safe_print(f"Canh bao: bo qua trang muc luc {current_url}: {exc}")
            continue

        for found in _get_catalog_urls(current_soup, current_url):
            found_key = _normalized_url(found)
            if found_key not in seen and found not in pending:
                pending.append(found)

        next_url = _next_catalog_url(current_soup, current_url)
        if next_url and _normalized_url(next_url) not in seen and next_url not in pending:
            pending.append(next_url)

        if pending:
            time.sleep(SLEEP_BETWEEN_PAGES)

    return sorted(expanded, key=_catalog_page_number)


def _chapter_containers(soup: BeautifulSoup) -> List:
    containers = []
    for selector in ("#list dl", "#list", "div[id^='content_']", ".chapter-list", ".listmain"):
        containers.extend(soup.select(selector))
    return containers or [soup]


def _extract_chapters_from_catalog(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    for container in _chapter_containers(soup):
        for a in container.select("a[href]"):
            title = _clean_spaces(a.get("title") or _text(a))
            url = _absolute_url(page_url, a.get("href", ""))
            if not title or not _is_chapter_url(page_url, url):
                continue
            url = _chapter_base_url(url)
            key = _normalized_url(url)
            if key in seen:
                continue
            seen.add(key)
            # Some title attributes are "Book Title Chapter Title"; prefer dd text.
            dd_text = _clean_spaces(_text(a.find("dd")))
            if dd_text:
                title = dd_text
            chapters.append({"title": title, "url": url})
    return chapters


def _find_catalog_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> str:
    for selector in ("#info_url[href]", "a[rel='index'][href]", ".bottem1 a[href]", ".bottem2 a[href]"):
        for a in soup.select(selector):
            href = _absolute_url(chapter_url, a.get("href", ""))
            if _is_catalog_url(href):
                return href
    return _catalog_url_from_book_url(_book_url_from_any(chapter_url), 1)


def getText(url: str = DEFAULT_URL) -> Dict:
    url = _ensure_url(url)
    local_input = _looks_like_local_file(url)
    soup = _fetch_html(url)
    original_url = url

    if not local_input and _is_chapter_url(url, url, allow_part=True):
        catalog_url = _find_catalog_url_from_chapter(soup, url)
        url = catalog_url
        soup = _fetch_html(catalog_url, referer=original_url)

    info = _get_book_info(soup, url)

    if local_input:
        catalog_soup = soup
        catalog_url = url
        catalog_urls = [url]
    else:
        catalog_url = url if _is_catalog_url(url) else _find_catalog_url(soup, info.get("url") or url)
        if _normalized_url(catalog_url) == _normalized_url(url):
            catalog_soup = soup
        else:
            catalog_soup = _fetch_html(catalog_url, referer=info.get("url") or url)
        catalog_urls = _expand_catalog_urls(catalog_soup, catalog_url) or [catalog_url]

    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    for item_url in catalog_urls:
        try:
            item_soup = catalog_soup if _normalized_url(item_url) == _normalized_url(catalog_url) else _fetch_html(item_url, referer=catalog_url)
        except Exception as exc:
            _safe_print(f"Canh bao: bo qua trang muc luc {item_url}: {exc}")
            continue
        for chapter in _extract_chapters_from_catalog(item_soup, item_url):
            key = _normalized_url(chapter["url"])
            if key in seen:
                continue
            seen.add(key)
            chapters.append(chapter)
        if not local_input:
            time.sleep(SLEEP_BETWEEN_PAGES)

    if chapters:
        info["latest_chapter"] = chapters[-1]["title"]
        info["latest_chapter_url"] = chapters[-1]["url"]
        info["total_chapters"] = len(chapters)

    return {**info, "chapters": chapters, "url": info.get("url") or _book_url_from_any(original_url)}


def _chapter_title_from_page(soup: BeautifulSoup, book_title: str = "", fallback: str = "") -> str:
    title = _text(soup.select_one("h1.bookname") or soup.select_one("h1.title") or soup.find("h1"))
    if not title:
        title_tag = _text(soup.find("title"))
        title = re.split(r"_|-", title_tag, maxsplit=1)[0].strip()
    title = _clean_spaces(title)
    title = re.sub(r"\s*[\uff08(]\s*\u7b2c?\s*\d+\s*\u9875\s*[\uff09)]\s*$", "", title)
    title = re.sub(r"\s*[\uff08(]\s*\d+\s*/\s*\d+\s*[\uff09)]\s*$", "", title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title) :].strip(" -_:\uff1a")
    title = re.sub(r"\s*[-_]?.*?\u7b14\u8da3\u5c0f\u8bf4.*$", "", title, flags=re.I).strip(" -_:\uff1a")
    return title or fallback or "Chapter"


def _decode_document_writeln(script_text: str) -> str:
    script_text = script_text or ""
    outputs: List[str] = []
    pattern = re.compile(
        r"document\.writeln\s*\(\s*[a-zA-Z_$][\w$]*\.[a-zA-Z_$][\w$]*\s*\(\s*(['\"])(?P<data>[A-Za-z0-9+/=]+)\1\s*\)\s*\)",
        flags=re.S,
    )
    for match in pattern.finditer(script_text):
        encoded = match.group("data")
        try:
            decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
        except Exception:
            continue
        if decoded:
            outputs.append(decoded)
    return "\n".join(outputs)


def _prepare_content_node(content) -> BeautifulSoup:
    content = BeautifulSoup(str(content), "html.parser")
    for script in list(content.find_all("script")):
        decoded_html = _decode_document_writeln(script.get_text("", strip=False))
        if decoded_html:
            script.replace_with(BeautifulSoup(decoded_html, "html.parser"))
        else:
            script.decompose()
    return content


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ").replace("\u3000", " ")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    trash_patterns = re.compile(
        r"(bqxs\.net|www\.bqxs|\u7b14\u8da3\u5c0f\u8bf4|\u672c\u7ad9|\u6700\u65b0\u57df\u540d|\u4e00\u79d2\u8bb0\u4f4f|"
        r"\u4e0a\u4e00\u7ae0|\u4e0b\u4e00\u7ae0|\u4e0a\u4e00\u9875|\u4e0b\u4e00\u9875|\u76ee\u5f55|\u4e66\u9875|"
        r"\u672c\u7ae0\u672a\u5b8c|\u7ee7\u7eed\u9605\u8bfb|\u52a0\u5165\u4e66\u67b6|\u8fd4\u56de\u9876\u90e8|"
        r"readSet|myJs|LastRead|document\.writeln|Copyright|\u5e7f\u544a)",
        flags=re.I,
    )

    lines: List[str] = []
    for raw_line in raw_text.split("\n"):
        line = _clean_spaces(raw_line)
        if not line or len(line) < 2:
            continue
        if title and line == title:
            continue
        if trash_patterns.search(line):
            continue
        if re.fullmatch(r"\(?https?://[^\s)]+\)?", line, flags=re.I):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return lines


def _extract_chapter_paragraphs(soup: BeautifulSoup, title: str = "") -> List[str]:
    content = (
        soup.select_one("#booktxt")
        or soup.select_one("#content")
        or soup.select_one(".content")
        or soup.select_one("article")
    )
    if not content:
        candidates = [
            node
            for node in soup.select(".chapter-content, .reader, .readcontent, main, article")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 120
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return []

    content = _prepare_content_node(content)
    for node in content.find_all(["style", "ins", "iframe", "select", "input", "button"]):
        node.decompose()
    for node in content.select(".ads, .ad, .readad, .bottem1, .bottem2, #content_1, #content_2, .text-danger"):
        node.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")

    return _clean_chapter_lines(content.get_text("\n", strip=False), title=title)


def _chapter_content_html_from_pages(pages: List[BeautifulSoup], title: str = "") -> Tuple[str, str]:
    paragraphs: List[str] = []
    for page in pages:
        paragraphs.extend(_extract_chapter_paragraphs(page, title=title))
    cleaned: List[str] = []
    for paragraph in paragraphs:
        if cleaned and cleaned[-1] == paragraph:
            continue
        cleaned.append(paragraph)
    text = "\n".join(cleaned).strip()
    content_html = "\n".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in cleaned)
    return content_html, text


def _next_part_url(soup: BeautifulSoup, current_url: str) -> str:
    current_id = _chapter_id_from_url(current_url)
    current_part = _chapter_part_number(current_url)
    candidates: List[str] = []

    for a in soup.select(".bottem1 a.next[href], .bottem2 a.next[href], a.next[href], a[href]"):
        text = _clean_spaces(_text(a))
        href = _absolute_url(current_url, a.get("href", ""))
        if not href or not _is_chapter_url(current_url, href, allow_part=True):
            continue
        if _chapter_id_from_url(href) != current_id:
            continue
        if _chapter_part_number(href) <= current_part:
            continue
        if "\u4e0b\u4e00\u9875" in text or "next" in (a.get("class") or []) or a.get("class") == "next":
            candidates.append(href)

    if not candidates:
        page_text = "\n".join(script.get_text("\n", strip=False) for script in soup.find_all("script"))
        for match in re.finditer(r"var\s+\w+\s*=\s*['\"](?P<href>/book/[^'\"]+?\.html?)['\"]", page_text):
            href = _absolute_url(current_url, match.group("href"))
            if _chapter_id_from_url(href) == current_id and _chapter_part_number(href) > current_part:
                candidates.append(href)

    return sorted(set(candidates), key=_chapter_part_number)[0] if candidates else ""


def _retry_delay_seconds(status_code: Optional[int], attempt: int) -> float:
    if status_code == 403:
        return min(14.0, 4.0 + attempt * 2.0)
    if status_code == 429:
        return min(20.0, 5.0 * attempt)
    return max(SLEEP_BETWEEN_CHAPS, 1.2 * attempt)


def fetch_chapter_content(
    url: str,
    retries: int = CHAPTER_RETRIES,
    *,
    fallback_title: str = "",
    book_title: str = "",
    referer: str = "",
) -> Dict:
    url = _ensure_url(url)
    local_input = _looks_like_local_file(url)
    last_status: Optional[int] = None
    for attempt in range(1, retries + 1):
        try:
            first_soup, status_code = _fetch_html_with_status(url, tries=1, referer=referer or _book_url_from_any(url))
            last_status = status_code if isinstance(status_code, int) else None
            title = _chapter_title_from_page(first_soup, book_title=book_title, fallback=fallback_title)

            pages = [first_soup]
            visited = {_normalized_url(url)}
            current_url = url
            current_soup = first_soup

            while len(pages) < MAX_CHAPTER_PARTS:
                next_url = _next_part_url(current_soup, current_url)
                if not next_url:
                    break
                if local_input and not _looks_like_local_file(next_url):
                    break
                key = _normalized_url(next_url)
                if key in visited:
                    break
                visited.add(key)
                time.sleep(SLEEP_BETWEEN_PAGES)
                current_soup = _fetch_html(next_url, tries=1, referer=_http_referer(current_url))
                current_url = next_url
                pages.append(current_soup)

            content_html, text = _chapter_content_html_from_pages(pages, title=title)
            if not _clean_spaces(text):
                raise FetchHtmlError("No chapter content", last_status)
            time.sleep(SLEEP_BETWEEN_CHAPS)
            return {
                "title": title,
                "content_html": content_html,
                "text": text,
                "url": url,
                "status_code": status_code,
                "parts": len(pages),
            }
        except FetchHtmlError as exc:
            last_status = exc.status_code
            if attempt < retries:
                time.sleep(_retry_delay_seconds(last_status, attempt))
        except Exception:
            if attempt < retries:
                time.sleep(_retry_delay_seconds(last_status, attempt))

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
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <article class="chapter">
{content_html}
  </article>
  {source}
</body>
</html>
"""


def save_all_chapters_to_html(
    title: str,
    chapters: List[Dict[str, str]],
    out_dir: str,
    start: int = 1,
    end: Optional[int] = None,
) -> List[str]:
    import download_policy

    return download_policy.save_all_chapters_to_html_with_retries(
        sys.modules[__name__],
        title,
        chapters,
        out_dir,
        start=start,
        end=end,
        fetch_fn=fetch_chapter_content,
    )


def _cover_extension_from_url(url: str, content_type: str = "") -> str:
    content_type = (content_type or "").lower()
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def _normalize_cover_image(content: bytes, ext: str) -> Tuple[bytes, str]:
    if not HAS_PILLOW:
        return content, ext
    try:
        img = Image.open(io.BytesIO(content))
        img.thumbnail(MAX_COVER_SIZE)
        if img.mode not in {"RGB", "L"}:
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90)
        return out.getvalue(), ".jpg"
    except Exception:
        return content, ext


def fetch_cover_from_book_page(book_page_url: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    soup = _fetch_html(book_page_url)
    info = _get_book_info(soup, book_page_url)
    cover_url = info.get("cover_url") or ""
    if not cover_url:
        return None, None, None
    try:
        response = _http_get(cover_url, referer=book_page_url, stream=True)
        status_code = getattr(response, "status_code", 200)
        if status_code != 200:
            return None, None, cover_url
        content = getattr(response, "content", b"")
        if not content:
            return None, None, cover_url
        ext = _cover_extension_from_url(cover_url, getattr(response, "headers", {}).get("content-type", ""))
        content, ext = _normalize_cover_image(content, ext)
        return content, ext, cover_url
    except Exception:
        return None, None, cover_url


def build_epub(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: str | Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
    cover_bytes: Optional[bytes] = None,
    cover_ext: Optional[str] = None,
) -> Path:
    import epub_builder
    import download_policy

    book_dir = Path(book_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    result = download_policy.download_chapters_with_retries(
        module=sys.modules[__name__],
        book_title=book_info.get("title") or "Book",
        chapters=chapters,
        out_dir=book_dir,
        fetch_fn=fetch_chapter_content,
        start=start,
        end=end,
        book_url=book_info.get("url", ""),
    )
    suffix = "" if result["start"] == 1 and result["end"] == len(chapters) else f"_{result['start']:04d}-{result['end']:04d}"
    epub_path = book_dir / f"{_safe_filename(book_info.get('title', 'Book'))}{suffix}.epub"
    with redirect_stdout(io.StringIO()):
        epub_builder.create_epub(
            book_url=book_info.get("url", ""),
            book_title=book_info.get("title") or "Book",
            author=book_info.get("author") or "Unknown",
            chapters=result["chapters"],
            fetch_fn=None,
            html_cache_dir=None,
            chapters_data=result["chapters_data"],
            cover_bytes=cover_bytes,
            cover_ext=cover_ext or ".jpg",
            out_epub_path=str(epub_path),
            tags=book_info.get("category") or book_info.get("genre"),
            book_info=book_info,
        )
    _safe_print(f"[Epub] Da tao xong ebook: {epub_path}")
    return epub_path


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Download bqxs.net novels")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--no-epub", action="store_true")
    parser.add_argument("-y", "--yes", action="store_true", help="Run non-interactively")
    args = parser.parse_args(argv)

    data = getText(args.url)
    title = data.get("title") or "Book"
    chapters = data.get("chapters") or []
    out_dir = OUTPUT_BASE / slugify_vi(title)
    out_dir.mkdir(parents=True, exist_ok=True)
    _safe_print(f"Title: {title}")
    _safe_print(f"Author: {data.get('author', 'Unknown')}")
    _safe_print(f"Chapters: {len(chapters)}")

    cover_bytes = cover_ext = None
    try:
        cover_bytes, cover_ext, _src = fetch_cover_from_book_page(data.get("url") or args.url)
    except Exception:
        cover_bytes, cover_ext = None, None

    if args.no_epub:
        save_all_chapters_to_html(title, chapters, str(out_dir), start=args.start, end=args.end)
    else:
        build_epub(data, chapters, out_dir, start=args.start, end=args.end, cover_bytes=cover_bytes, cover_ext=cover_ext)


try:
    from download_policy import install_adapter_policy as _install_adapter_policy

    _install_adapter_policy(globals())
except Exception:
    pass


if __name__ == "__main__":
    main()
