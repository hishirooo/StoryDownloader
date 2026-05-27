# -*- coding: utf-8 -*-
"""
Downloader for https://www.22biqu.com/.

Book page sample:
  https://www.22biqu.com/biqu117731/

Chapter page sample:
  https://www.22biqu.com/biqu117731/50946677.html
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

try:
    from PIL import Image

    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

BASE_URL = "https://www.22biqu.com/"
DEFAULT_URL = "https://www.22biqu.com/biqu117731/"
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
SLEEP_BETWEEN_CHAPS = 0.6
CHAPTER_RETRIES = 4
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)
MAX_CHAPTER_PARTS = 20


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
    if not value or re.match(r"^[a-z][a-z0-9+.-]*://", value, flags=re.I):
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
        return "https://" + url.lstrip("/")
    return url


def _absolute_url(page_url: str, href: str) -> str:
    href = html.unescape(href or "").strip()
    if not href or href.startswith("javascript:"):
        return ""
    if href.startswith("//"):
        return "https:" + href
    if _looks_like_local_file(page_url):
        if re.match(r"^https?://", href, flags=re.I):
            return href
        if href.startswith("./") or href.startswith("../"):
            return str((Path(page_url).parent / href).resolve())
        if not re.match(r"^[a-z][a-z0-9+.-]*:", href, flags=re.I):
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
    candidates.extend(["utf-8", "gb18030", "gbk"])
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


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_spaces(tag["content"])
    return ""


def _book_id_from_url(url: str) -> str:
    match = re.search(r"/biqu(\d+)/", urlparse(url).path)
    return match.group(1) if match else ""


def _book_dir_from_url(url: str) -> str:
    match = re.search(r"(/biqu\d+/)", urlparse(url).path)
    return match.group(1) if match else ""


def _is_chapter_url(page_url: str, chapter_url: str) -> bool:
    target = urlparse(chapter_url)
    if target.netloc and urlparse(page_url).netloc and target.netloc != urlparse(page_url).netloc:
        return False
    if not re.search(r"/biqu\d+/\d+(?:_\d+)?\.html$", target.path):
        return False
    page_book_id = _book_id_from_url(page_url)
    target_book_id = _book_id_from_url(chapter_url)
    return not page_book_id or not target_book_id or page_book_id == target_book_id


def _is_catalog_url(url: str) -> bool:
    return bool(re.search(r"/biqu\d+/(?:\d+/)?$", urlparse(url).path))


def _chapter_number(title: str) -> int:
    match = re.search(r"\u7b2c\s*(\d+)\s*\u7ae0", title or "")
    return int(match.group(1)) if match else 0


def _chapter_base_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"_(\d+)(\.html)$", r"\2", parsed.path)
    return parsed._replace(path=path, query="", fragment="").geturl()


def _get_book_info(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, str]:
    title = (
        _meta_content(soup, "og:novel:book_name", "og:title")
        or _text(soup.select_one(".info .top h1"))
        or _text(soup.select_one(".info h1"))
        or re.sub(r"\(.*$", "", _text(soup.find("title")))
    )
    author = _meta_content(soup, "og:novel:author")
    category = _meta_content(soup, "og:novel:category")
    status = _meta_content(soup, "og:novel:status")
    update_time = _meta_content(soup, "og:novel:update_time")
    latest_chapter = _meta_content(soup, "og:novel:lastest_chapter_name")
    latest_url = _meta_content(soup, "og:novel:lastest_chapter_url")
    cover_url = _meta_content(soup, "og:image")
    intro = _meta_content(soup, "description")

    for p in soup.select(".info p, .top p"):
        text = _clean_spaces(p.get_text(" ", strip=True))
        if not author and "\u4f5c" in text and "\u8005" in text:
            author = re.sub(r"^.*?\uff1a", "", text).strip()
        if not category and "\u7c7b" in text and "\u522b" in text:
            category = re.sub(r"^.*?\uff1a", "", text).strip()
        if not status and "\u72b6" in text and "\u6001" in text:
            status = re.sub(r"^.*?\uff1a", "", text).strip()
        if not update_time and "\u66f4\u65b0\u65f6\u95f4" in text:
            update_time = re.sub(r"^.*?\uff1a", "", text).strip()
        if not latest_chapter and "\u6700\u65b0\u7ae0\u8282" in text:
            latest_chapter = re.sub(r"^.*?\uff1a", "", text).strip()

    if latest_url:
        latest_url = _absolute_url(page_url, latest_url)
    if cover_url:
        cover_url = _absolute_url(page_url, cover_url)
    else:
        img = soup.select_one(".imgbox img[src], .book-img img[src], .info img[src]")
        if img:
            cover_url = _absolute_url(page_url, img.get("src", ""))

    book_url = _meta_content(soup, "og:novel:url", "og:novel:read_url")
    canonical = soup.select_one("link[rel='canonical'][href]")
    if not book_url and canonical and _is_catalog_url(canonical.get("href", "")):
        book_url = canonical.get("href", "")
    if not book_url:
        for a in soup.select(".layout-tit a[href], .con_top a[href]"):
            href = _absolute_url(page_url, a.get("href", ""))
            if _is_catalog_url(href):
                book_url = href
    if not book_url:
        book_url = page_url
    book_url = _absolute_url(page_url, book_url)

    page_text = _clean_spaces(soup.get_text(" ", strip=True))
    total_chapters = 0
    nums = [_chapter_number(a.get_text(" ", strip=True)) for a in soup.select("a[href]")]
    nums = [n for n in nums if n > 0]
    if nums:
        total_chapters = max(nums)
    match = re.search(r"\u5171\s*(\d+)\s*\u7ae0", page_text)
    if match:
        total_chapters = max(total_chapters, int(match.group(1)))

    return {
        "title": _clean_spaces(title) or "Unknown",
        "author": _clean_spaces(author) or "Unknown",
        "category": _clean_spaces(category),
        "genre": _clean_spaces(category),
        "status": _clean_spaces(status),
        "update_time": _clean_spaces(update_time),
        "latest_chapter": _clean_spaces(latest_chapter),
        "latest_chapter_url": latest_url,
        "intro": _clean_spaces(intro),
        "cover_url": cover_url,
        "url": book_url,
        "book_id": _book_id_from_url(book_url),
        "total_chapters": total_chapters,
    }


def _catalog_page_number(url: str) -> int:
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"/biqu\d+/(\d+)$", path)
    return int(match.group(1)) if match else 1


def _get_catalog_urls(soup: BeautifulSoup, page_url: str) -> List[str]:
    urls: List[str] = []
    if _is_catalog_url(page_url):
        urls.append(page_url)
    for option in soup.select("select#indexselect option[value]"):
        href = _absolute_url(page_url, option.get("value", ""))
        if href and _is_catalog_url(href):
            urls.append(href)
    for a in soup.select(".index-container a[href]"):
        href = _absolute_url(page_url, a.get("href", ""))
        if href and _is_catalog_url(href):
            urls.append(href)

    seen = set()
    result = []
    for url in urls:
        key = _normalized_url(url)
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
    return sorted(result, key=_catalog_page_number)


def _extract_chapters_from_catalog(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    containers = soup.select("ul.section-list")
    if not containers:
        containers = soup.select("#list, .chapter-list, .listmain")
    for container in containers:
        for a in container.select("a[href]"):
            title = _clean_spaces(a.get_text(" ", strip=True))
            url = _absolute_url(page_url, a.get("href", ""))
            if not title or not _is_chapter_url(page_url, url):
                continue
            chapters.append({"title": title, "url": _chapter_base_url(url)})
    return chapters


def _book_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> str:
    for a in soup.select(".layout-tit a[href], .con_top a[href]"):
        href = _absolute_url(chapter_url, a.get("href", ""))
        if _is_catalog_url(href):
            return href
    book_dir = _book_dir_from_url(chapter_url)
    if book_dir:
        parsed = urlparse(chapter_url)
        return parsed._replace(path=book_dir, query="", fragment="").geturl()
    return DEFAULT_URL


def getText(url: str = DEFAULT_URL) -> Dict:
    url = _ensure_url(url)
    local_input = _looks_like_local_file(url)
    soup = _fetch_html(url)

    if _is_chapter_url(url, url):
        book_url = _book_url_from_chapter(soup, url)
        if not local_input:
            soup = _fetch_html(book_url, referer=_http_referer(url))
            url = book_url

    info = _get_book_info(soup, url)
    catalog_urls = [url] if local_input else _get_catalog_urls(soup, str(info.get("url") or url))
    chapters_by_key: Dict[str, Dict[str, str]] = {}

    if not catalog_urls:
        catalog_urls = [str(info.get("url") or url)]

    for catalog_url in catalog_urls:
        if local_input and _normalized_url(catalog_url) != _normalized_url(url):
            continue
        try:
            catalog_soup = soup if _normalized_url(catalog_url) == _normalized_url(url) else _fetch_html(catalog_url, referer=info.get("url") or url)
        except Exception as exc:
            _safe_print(f"Canh bao: bo qua trang muc luc {catalog_url}: {exc}")
            continue
        for chapter in _extract_chapters_from_catalog(catalog_soup, catalog_url):
            key = _normalized_url(chapter["url"])
            chapters_by_key[key] = chapter
        if not local_input and _normalized_url(catalog_url) != _normalized_url(url):
            time.sleep(SLEEP_BETWEEN_PAGES)

    chapters = list(chapters_by_key.values())
    chapters.sort(key=lambda item: (_chapter_number(item.get("title", "")) or 10**9, item.get("url", "")))

    if chapters:
        info["latest_chapter"] = chapters[-1]["title"]
        info["latest_chapter_url"] = chapters[-1]["url"]
        info["total_chapters"] = len(chapters)
    return {**info, "chapters": chapters}


def _chapter_title_from_page(soup: BeautifulSoup, fallback: str = "") -> str:
    title = _text(soup.select_one("h1.title") or soup.select_one(".reader-main h1") or soup.select_one("h1"))
    if not title:
        title = _text(soup.find("title"))
        title = re.sub(r"_.*$", "", title)
    return _clean_spaces(title) or fallback or "\u7ae0\u8282"


def _next_part_url(soup: BeautifulSoup, current_url: str) -> str:
    for a in soup.select("a[href]"):
        text = _clean_spaces(a.get_text(" ", strip=True))
        if text == "\u4e0b\u4e00\u9875":
            href = _absolute_url(current_url, a.get("href", ""))
            if href and _chapter_base_url(href) == _chapter_base_url(current_url):
                return href
    return ""


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    trash = re.compile(
        r"(www\.22biqu\.com|\u7b14\u8da3\u9601|\u6700\u65b0\u5730\u5740|\u8bb0\u4f4f\u672c\u7ad9|"
        r"\u624b\u673a\u9605\u8bfb|\u4e0a\u4e00\u7ae0|\u4e0b\u4e00\u9875|\u4e0b\u4e00\u7ae0|\u76ee\u5f55|"
        r"\u540c\u7c7b\u70ed\u95e8|\u70b9\u51fb\u4e0b\u8f7d)",
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
    content = soup.select_one("#content") or soup.select_one(".content")
    if not content:
        candidates = [
            node for node in soup.select(".reader-main, article, main")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 500
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return []

    content = BeautifulSoup(str(content), "html.parser")
    for node in content.find_all(["script", "style", "ins", "iframe", "select", "input"]):
        node.decompose()
    for node in content.select(".ads, .ad, .readad, .reader-fun, .section-opt, .content-tip"):
        node.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")
    return _clean_chapter_lines(content.get_text("\n", strip=False), title=title)


def _chapter_content_html_from_pages(pages: List[BeautifulSoup], title: str) -> Tuple[str, str]:
    paragraphs: List[str] = []
    for page in pages:
        paragraphs.extend(_extract_chapter_paragraphs(page, title=title))
    seen_blank = False
    cleaned: List[str] = []
    for paragraph in paragraphs:
        if paragraph:
            cleaned.append(paragraph)
            seen_blank = False
        elif not seen_blank:
            seen_blank = True
    text = "\n".join(cleaned)
    content_html = "\n".join(f"<p>{html.escape(p)}</p>" for p in cleaned) or "<p>(Khong co noi dung)</p>"
    return content_html, text


def fetch_chapter_content(
    url: str,
    retries: int = CHAPTER_RETRIES,
    *,
    fallback_title: str = "",
    book_title: str = "",
) -> Dict:
    url = _ensure_url(url)
    local_input = _looks_like_local_file(url)
    last_status: Optional[int] = None
    for attempt in range(1, retries + 1):
        try:
            first_soup, status_code = _fetch_html_with_status(url, tries=1, referer=BASE_URL)
            last_status = status_code if isinstance(status_code, int) else None
            title = _chapter_title_from_page(first_soup, fallback=fallback_title)
            pages = [first_soup]
            seen = {_normalized_url(url)}
            current_url = url
            current_soup = first_soup

            while len(pages) < MAX_CHAPTER_PARTS:
                next_url = _next_part_url(current_soup, current_url)
                if not next_url:
                    break
                if local_input and not _looks_like_local_file(next_url):
                    break
                key = _normalized_url(next_url)
                if key in seen:
                    break
                seen.add(key)
                time.sleep(SLEEP_BETWEEN_PAGES)
                current_soup = _fetch_html(next_url, referer=_http_referer(current_url))
                current_url = next_url
                pages.append(current_soup)

            content_html, text = _chapter_content_html_from_pages(pages, title)
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
<html lang="zh-CN">
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
    html_dir = Path(book_dir) / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    suffix = f" - {_safe_filename(title, 100)}" if title else ""
    return html_dir / f"{idx:04d}{suffix}.html"


def _find_cached_chapter_path(book_dir: str | Path, idx: int) -> Optional[Path]:
    html_dir = Path(book_dir) / "html"
    if not html_dir.exists():
        return None
    matches = sorted(html_dir.glob(f"{idx:04d}*.html"))
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
    html_path = _chapter_html_path(book_dir, idx, data.get("title") or f"Chapter {idx}")
    html_path.write_text(_chapter_html_doc(data.get("title") or f"Chapter {idx}", data.get("content_html") or "", data.get("url") or source_url), encoding="utf-8")
    data["html_path"] = str(html_path)
    return html_path


def _save_chapter_html(chapter: Dict[str, str], idx: int, book_dir: str | Path, *, force: bool = False) -> Dict[str, str]:
    cached = _find_cached_chapter_path(book_dir, idx)
    if cached and not force:
        data = _read_cached_chapter(cached)
        if not _is_failed_chapter_data(data):
            return data
    data = fetch_chapter_content(chapter["url"], fallback_title=chapter.get("title", ""))
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
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: str | Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
    force: bool = False,
) -> List[Dict[str, str]]:
    start, end = _normalize_range(len(chapters), start, end)
    selected_total = end - start + 1
    downloaded: List[Dict[str, str]] = []
    _safe_print(f"Bat dau tai/cache {selected_total} chuong vao: {book_dir}")
    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        data = _save_chapter_html(chapter, idx, book_dir, force=force)
        downloaded.append(data)
        _safe_print(
            chapter_log_line(
                done,
                selected_total,
                data.get("status_code", "ERR"),
                idx,
                len(chapters),
                data.get("title") or chapter.get("title") or "",
            )
        )
    _safe_print(f"Hoan tat tai/cache {selected_total} chuong.")
    return downloaded


def save_all_chapters_to_html(title: str, chapters: List[Dict[str, str]], out_dir: str, start: int = 1, end=None) -> None:
    download_chapters({"title": title}, chapters, out_dir, start=start, end=end)


def _selected_chapter_data(
    book_info: Dict[str, str],
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

    book_dir = Path(book_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    start, end = _normalize_range(len(chapters), start, end)
    selected_chapters = chapters[start - 1 : end]
    chapters_data = _selected_chapter_data(book_info, chapters, book_dir, start=start, end=end)
    suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    epub_path = book_dir / f"{_safe_filename(book_info.get('title', 'Truyen'))}{suffix}.epub"

    noise = io.StringIO()
    with redirect_stdout(noise):
        epub_builder.create_epub(
            book_url=book_info.get("url", ""),
            book_title=book_info.get("title", "Truyen"),
            author=book_info.get("author", "Unknown"),
            chapters=selected_chapters,
            fetch_fn=fetch_chapter_content,
            cover_bytes=cover_bytes,
            cover_ext=cover_ext or ".jpg",
            out_epub_path=str(epub_path),
            html_cache_dir=None,
            chapters_data=chapters_data,
            language="zh-CN",
            tags=book_info.get("category", ""),
            book_info=book_info,
        )
    _safe_print(f"[Epub] Da tao xong ebook: {epub_path}")
    return epub_path


def _download_cover(cover_url: str) -> Tuple[Optional[bytes], Optional[str]]:
    if not cover_url or cover_url.startswith("data:"):
        return None, None
    if _looks_like_local_file(cover_url):
        path = Path(cover_url)
        return path.read_bytes(), path.suffix.lower() or ".jpg"
    try:
        response = _http_get(cover_url)
        response.raise_for_status()
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
        return response.content, ext
    except Exception as exc:
        _safe_print(f"Khong tai duoc cover: {exc}")
        return None, None


def _resize_cover(content: bytes, ext: str) -> Tuple[bytes, str]:
    if not content or not HAS_PILLOW:
        return content, ext
    try:
        image = Image.open(io.BytesIO(content)).convert("RGB")
        image.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88)
        return output.getvalue(), ".jpg"
    except Exception as exc:
        _safe_print(f"Khong xu ly duoc cover bang Pillow: {exc}")
        return content, ext


def _load_cover(book_info: Dict[str, str], book_dir: Path) -> Tuple[Optional[bytes], Optional[str]]:
    cover_url = book_info.get("cover_url") or ""
    cover_bytes, cover_ext = _download_cover(cover_url)
    if cover_bytes and cover_ext:
        cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
        cover_path = book_dir / f"cover{cover_ext}"
        cover_path.write_bytes(cover_bytes)
        _safe_print(f"Da luu cover: {cover_path}")
    return cover_bytes, cover_ext


def fetch_cover_from_book_page(book_page_url: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    book_page_url = _ensure_url(book_page_url)
    soup = _fetch_html(book_page_url)
    page_url = book_page_url
    if _is_chapter_url(book_page_url, book_page_url):
        book_url = _book_url_from_chapter(soup, book_page_url)
        soup = _fetch_html(book_url, referer=_http_referer(book_page_url))
        page_url = book_url
    info = _get_book_info(soup, page_url)
    cover_url = info.get("cover_url") or ""
    cover_bytes, cover_ext = _download_cover(cover_url)
    if cover_bytes and cover_ext:
        cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
        return cover_bytes, cover_ext, cover_url or None
    return None, None, cover_url or None


def _prepare_book_dir(book_info: Dict[str, str]) -> Path:
    book_dir = OUTPUT_BASE / _safe_filename(book_info.get("title") or "Truyen")
    book_dir.mkdir(parents=True, exist_ok=True)
    return book_dir


def _save_book_info(book_info: Dict[str, str], chapters: List[Dict[str, str]], book_dir: Path) -> None:
    lines = [
        f"Title: {book_info.get('title', '')}",
        f"Author: {book_info.get('author', '')}",
        f"Status: {book_info.get('status', '')}",
        f"Category: {book_info.get('category', '')}",
        f"Update time: {book_info.get('update_time', '')}",
        f"URL: {book_info.get('url', '')}",
        f"Cover: {book_info.get('cover_url', '')}",
        f"Chapters: {len(chapters)}",
        "",
        book_info.get("intro", ""),
        "",
        "Muc luc:",
    ]
    for idx, chapter in enumerate(chapters, 1):
        lines.append(f"{idx:04d}. {chapter['title']} - {chapter['url']}")
    (book_dir / "book_info.txt").write_text("\n".join(lines), encoding="utf-8")


def _load_book_context(url: str) -> Tuple[Dict[str, str], List[Dict[str, str]], Path, Optional[bytes], Optional[str]]:
    if not url:
        raise ValueError("Can nhap URL truyen hoac chuong.")
    _safe_print("Dang lay thong tin truyen...")
    data = getText(url)
    book_info = {
        "title": data.get("title") or "Unknown",
        "author": data.get("author") or "Unknown",
        "status": data.get("status", ""),
        "category": data.get("category", ""),
        "update_time": data.get("update_time", ""),
        "latest_chapter": data.get("latest_chapter", ""),
        "latest_chapter_url": data.get("latest_chapter_url", ""),
        "intro": data.get("intro", ""),
        "cover_url": data.get("cover_url", ""),
        "url": data.get("url", url),
    }
    chapters = data.get("chapters", [])
    book_dir = _prepare_book_dir(book_info)
    _save_book_info(book_info, chapters, book_dir)
    epub_preview_path = book_dir / f"{_safe_filename(book_info['title'])}.epub"

    _safe_print("\n-----------------Thong tin truyen-----------------")
    _safe_print(f"Ten truyen    : {book_info['title']}")
    _safe_print(f"Tac gia       : {book_info['author']}")
    if book_info.get("status"):
        _safe_print(f"Trang thai    : {book_info['status']}")
    if book_info.get("category"):
        _safe_print(f"The loai      : {book_info['category']}")
    _safe_print(f"So chuong     : {len(chapters)}")
    if book_info.get("latest_chapter"):
        _safe_print(f"Moi nhat      : {book_info['latest_chapter']}")
    _safe_print(f"Thu muc truyen: {book_dir}")
    _safe_print(f"EPUB se luu   : {epub_preview_path}")

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
            _safe_print("Vui long nhap so hop le.")


def _print_download_menu() -> None:
    _safe_print("\n-----------------Menu-----------------")
    _safe_print("[1] Tai tat ca (HTML + EPUB) (mac dinh)")
    _safe_print("[2] Tai tu X toi Y (HTML)")
    _safe_print("[3] Tai 1 chuong (HTML)")
    _safe_print("[4] Tao EPUB tu cache")
    _safe_print("[5] Thoat")


def _post_task_menu() -> bool:
    _safe_print("\n-----------------Menu-----------------")
    _safe_print("[1] Nhap URL truyen moi")
    _safe_print("[2] Thoat (mac dinh)")
    choice = input("Chon [2]: ").strip() or "2"
    return choice == "1"


def _run_once(args: argparse.Namespace) -> None:
    book_info, chapters, book_dir, cover_bytes, cover_ext = _load_book_context(args.url)
    if not chapters:
        raise RuntimeError("Khong tim thay chuong")
    start, end = _normalize_range(len(chapters), args.start, args.end)
    download_chapters(book_info, chapters, book_dir, start=start, end=end, force=args.force)
    if not args.no_epub:
        build_epub(book_info, chapters, book_dir, start=start, end=end, cover_bytes=cover_bytes, cover_ext=cover_ext)


def _interactive_main() -> None:
    _safe_print("Downloader 22biqu.com")
    while True:
        raw_url = input("Nhap URL (bo trong de thoat): ").strip()
        if not raw_url:
            return
        try:
            book_info, chapters, book_dir, cover_bytes, cover_ext = _load_book_context(raw_url)
            if not chapters:
                _safe_print("Khong tim thay chuong.")
                continue
        except Exception as exc:
            _safe_print(f"Loi: {exc}")
            continue

        while True:
            _print_download_menu()
            choice = input("Chon [1]: ").strip() or "1"
            try:
                if choice == "1":
                    download_chapters(book_info, chapters, book_dir)
                    build_epub(book_info, chapters, book_dir, cover_bytes=cover_bytes, cover_ext=cover_ext)
                    break
                if choice == "2":
                    start = _ask_int("Chuong bat dau: ")
                    end = _ask_int("Chuong ket thuc: ", len(chapters))
                    start, end = _normalize_range(len(chapters), start, end)
                    download_chapters(book_info, chapters, book_dir, start=start, end=end)
                    break
                if choice == "3":
                    idx = _ask_int("Chuong can tai: ")
                    idx, _ = _normalize_range(len(chapters), idx, idx)
                    download_chapters(book_info, chapters, book_dir, start=idx, end=idx)
                    break
                if choice == "4":
                    start = _ask_int("Chuong bat dau [1]: ", 1)
                    end = _ask_int(f"Chuong ket thuc [{len(chapters)}]: ", len(chapters))
                    start, end = _normalize_range(len(chapters), start, end)
                    build_epub(book_info, chapters, book_dir, start=start, end=end, cover_bytes=cover_bytes, cover_ext=cover_ext)
                    break
                if choice == "5":
                    return
                _safe_print("Lua chon khong hop le.")
            except Exception as exc:
                _safe_print(f"Loi: {exc}")

        if not _post_task_menu():
            return


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Download 22biqu.com novel chapters and build EPUB.")
    parser.add_argument("url", nargs="?", help="Book/chapter URL. Omit to open menu.")
    parser.add_argument("--start", type=int, default=1, help="Start chapter index")
    parser.add_argument("--end", type=int, default=None, help="End chapter index")
    parser.add_argument("--force", action="store_true", help="Refetch even when cache exists")
    parser.add_argument("--no-epub", action="store_true", help="Only download/cache HTML")
    parser.add_argument("-y", "--yes", action="store_true", help="Run non-interactively")
    args = parser.parse_args(argv)

    if args.yes and not args.url:
        parser.error("-y/--yes can dung kem URL")
    if args.yes or args.url:
        _run_once(args)
    else:
        _interactive_main()



try:
    from download_policy import install_adapter_policy as _install_adapter_policy
    _install_adapter_policy(globals())
except Exception:
    pass
if __name__ == "__main__":
    main()
