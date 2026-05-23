# -*- coding: utf-8 -*-
"""
Downloader for https://www.zhaoshuyuan.net/ (uu看书 skin).

Book page sample:
  https://www.zhaoshuyuan.net/book/icsqtns

Catalog page sample:
  https://www.zhaoshuyuan.net/index/1880/icsqtns/1.html/

Chapter page sample:
  https://www.zhaoshuyuan.net/read/icsqtns/scngb.html
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


BASE_URL = "https://www.zhaoshuyuan.net/"
DEFAULT_URL = "https://www.zhaoshuyuan.net/book/icsqtns"
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
SLEEP_BETWEEN_PAGES = 0.6
SLEEP_BETWEEN_CHAPS = 0.8
CHAPTER_RETRIES = 5
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)


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
        value = re.sub(rf"^{re.escape(label)}\s*[:：]?\s*", "", value).strip()
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
    href = html.unescape(href or "").strip().replace("\\/", "/")
    if not href:
        return page_url
    if href.startswith("//"):
        scheme = urlparse(page_url).scheme or "https"
        return f"{scheme}:{href}"
    base = page_url if urlparse(page_url).scheme else BASE_URL
    return urljoin(base, href)


def _normalized_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return str(Path(url))
    return parsed._replace(fragment="", query="").geturl().rstrip("/")


def _http_get(url: str, referer: Optional[str] = None):
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

    encoding = getattr(response, "encoding", None) if response is not None else None
    apparent = getattr(response, "apparent_encoding", None) if response is not None else None
    if encoding:
        candidates.append(encoding)
    if apparent:
        candidates.append(apparent)

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


def _field_from_bookdes(soup: BeautifulSoup, label: str) -> str:
    for p in soup.select(".bookdes p"):
        text = _clean_spaces(p.get_text(" ", strip=True))
        if label not in text:
            continue
        text = _strip_label(text, label)
        if "|" in text:
            text = text.split("|", 1)[0].strip()
        return text
    return ""


def _find_book_url(soup: BeautifulSoup, page_url: str) -> str:
    meta_url = _meta_content(soup, "og:novel:read_url", "og:novel:url")
    if "/book/" in meta_url:
        return _absolute_url(page_url, meta_url)
    for selector in [
        ".breadcrumb a[href*='/book/']",
        ".booktitle a[href*='/book/']",
        ".bookbtn a[href*='/book/']",
        "a[href*='/book/']",
    ]:
        node = soup.select_one(selector)
        if node and node.get("href"):
            return _absolute_url(page_url, node["href"])
    return ""


def _find_catalog_url(soup: BeautifulSoup, page_url: str) -> str:
    for selector in [
        ".bookbtn a[href*='/index/']",
        ".morechapter[href*='/index/']",
        "a[rel='index'][href*='/index/']",
        "a[href*='/index/']",
    ]:
        node = soup.select_one(selector)
        if node and node.get("href"):
            return _absolute_url(page_url, node["href"])
    return ""


def _clean_intro(value: str) -> str:
    value = _clean_spaces(value)
    value = re.sub(r"^《[^》]+》", "", value).strip()
    value = _strip_label(value, "小说简介", "内容简介", "简介")
    return value


def _get_book_info(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, str]:
    title = (
        _meta_content(soup, "og:novel:book_name", "og:title")
        or _text(soup.select_one(".book .booktitle h1"))
        or _text(soup.select_one(".chapters .booktitle h1"))
        or _text(soup.select_one(".booktitle h1"))
        or _text(soup.find("h1"))
    )
    if not title:
        title_tag = _text(soup.find("title"))
        title = re.split(r"章节列表|最新章节|全文免费阅读|-|_", title_tag, maxsplit=1)[0].strip()

    author = _meta_content(soup, "og:novel:author") or _field_from_bookdes(soup, "作者")
    if not author:
        h2_text = _text(soup.select_one(".chapters .booktitle h2"))
        match = re.search(r"作者\s*[:：]\s*([^|]+)", h2_text)
        if match:
            author = _clean_spaces(match.group(1))

    category = _meta_content(soup, "og:novel:category")
    if not category:
        links = soup.select(".breadcrumb a")
        if len(links) >= 2:
            category = _text(links[1])

    status = _meta_content(soup, "og:novel:status") or _field_from_bookdes(soup, "状态")
    if not status:
        h2_text = _text(soup.select_one(".chapters .booktitle h2"))
        match = re.search(r"状态\s*[:：]\s*([^|]+)", h2_text)
        if match:
            status = _clean_spaces(match.group(1))

    update_time = _meta_content(soup, "og:novel:update_time") or _field_from_bookdes(soup, "最后更新")
    latest_chapter = (
        _meta_content(soup, "og:novel:lastest_chapter_name", "og:novel:latest_chapter_name")
        or _text(soup.select_one(".bookdes a[rel='chapter']"))
    )
    latest_url = _meta_content(soup, "og:novel:lastest_chapter_url", "og:novel:latest_chapter_url")
    latest_node = soup.select_one(".bookdes a[rel='chapter']")
    if not latest_url and latest_node and latest_node.get("href"):
        latest_url = latest_node["href"]
    if latest_url:
        latest_url = _absolute_url(page_url, latest_url)

    intro = _clean_intro(_meta_content(soup, "og:description", "description"))
    intro_node = soup.select_one(".bookintro")
    if intro_node:
        intro = _clean_intro(intro_node.get_text("\n", strip=True))

    cover_url = _meta_content(soup, "og:image")
    if not cover_url:
        img = soup.select_one(".cover img[src], .book img[src]")
        if img:
            cover_url = img.get("src", "").strip()
    if cover_url:
        cover_url = _absolute_url(page_url, cover_url)

    return {
        "title": _clean_spaces(title) or "Unknown",
        "author": _clean_spaces(author) or "Unknown",
        "status": _clean_spaces(status),
        "category": _clean_spaces(category),
        "update_time": _clean_spaces(update_time),
        "latest_chapter": _clean_spaces(latest_chapter),
        "latest_chapter_url": latest_url or "",
        "cover_url": cover_url or "",
        "intro": intro or "",
        "catalog_url": _find_catalog_url(soup, page_url),
        "book_url": _find_book_url(soup, page_url),
        "url": page_url,
    }


def _book_key_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path
    for pattern in (r"/book/([^/]+)", r"/read/([^/]+)/[^/]+\.html?", r"/index/\d+/([^/]+)/\d+\.html/?"):
        match = re.search(pattern, path, flags=re.I)
        if match:
            return match.group(1)
    return None


def _is_catalog_url(url: str) -> bool:
    path = urlparse(url).path.rstrip("/")
    return bool(re.search(r"/index/\d+/[^/]+/\d+\.html$", path, flags=re.I))


def _is_chapter_url(page_url: str, chapter_url: str, book_key: Optional[str] = None) -> bool:
    page = urlparse(page_url if urlparse(page_url).scheme else BASE_URL)
    target = urlparse(chapter_url)
    if target.netloc and page.netloc and target.netloc != page.netloc:
        return False
    if not re.fullmatch(r"/read/[^/]+/[^/]+\.html?", target.path, flags=re.I):
        return False
    target_key = _book_key_from_url(chapter_url)
    return not book_key or not target_key or target_key == book_key


def _href_from_node(a) -> str:
    href = html.unescape(a.get("href", "") or "").strip()
    if href and not href.lower().startswith("javascript"):
        return href
    onclick = html.unescape(a.get("onclick", "") or "")
    match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", onclick, flags=re.I)
    if match:
        return match.group(1).replace("\\/", "/")
    return href


def _catalog_page_number(url: str) -> int:
    match = re.search(r"/(\d+)\.html$", urlparse(url).path.rstrip("/"), flags=re.I)
    return int(match.group(1)) if match else 0


def _get_catalog_urls(soup: BeautifulSoup, page_url: str) -> List[str]:
    urls: List[str] = []
    if _is_catalog_url(page_url):
        urls.append(page_url)

    for option in soup.select("select#indexselect option[value], select option[value*='/index/']"):
        urls.append(_absolute_url(page_url, option.get("value", "")))

    for a in soup.select("a[href*='/index/']"):
        urls.append(_absolute_url(page_url, a.get("href", "")))

    catalog_url = _find_catalog_url(soup, page_url)
    if catalog_url:
        urls.append(catalog_url)

    seen = set()
    result: List[str] = []
    for url in sorted(urls, key=lambda item: (_catalog_page_number(item), urls.index(item))):
        key = _normalized_url(url)
        if not key or key in seen or not _is_catalog_url(url):
            continue
        seen.add(key)
        result.append(url)
    return result


def _expand_catalog_urls(soup: BeautifulSoup, page_url: str) -> List[str]:
    catalog_urls = _get_catalog_urls(soup, page_url)
    if not catalog_urls:
        return []

    expanded: List[str] = []
    seen: set[str] = set()
    for catalog_url in catalog_urls:
        key = _normalized_url(catalog_url)
        if key not in seen:
            seen.add(key)
            expanded.append(catalog_url)

        if _normalized_url(catalog_url) == _normalized_url(page_url) and soup.select_one("select#indexselect option[value]"):
            catalog_soup = soup
        else:
            try:
                catalog_soup = _fetch_html(catalog_url, referer=page_url)
            except Exception as exc:
                _safe_print(f"Canh bao: khong mo duoc muc luc de mo rong {catalog_url}: {exc}")
                continue

        for option_url in _get_catalog_urls(catalog_soup, catalog_url):
            option_key = _normalized_url(option_url)
            if option_key in seen:
                continue
            seen.add(option_key)
            expanded.append(option_url)
        time.sleep(SLEEP_BETWEEN_PAGES)

    return sorted(expanded, key=lambda item: _catalog_page_number(item))


def _get_list_chapters(soup: BeautifulSoup, page_url: str, book_key: Optional[str] = None) -> List[Dict[str, str]]:
    containers = [
        node
        for node in [
            soup.select_one(".chapterlist .all"),
            soup.select_one(".chapters .chapterlist"),
            soup.select_one(".chapterlist"),
        ]
        if node is not None
    ]
    if not containers:
        containers = [soup]

    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    for container in containers:
        for a in container.find_all("a"):
            raw_href = _href_from_node(a)
            if not raw_href:
                continue
            url = _absolute_url(page_url, raw_href)
            title = _clean_spaces(a.get("title") or _text(a))
            if not title or not _is_chapter_url(page_url, url, book_key=book_key):
                continue
            key = _normalized_url(url)
            if key in seen:
                continue
            seen.add(key)
            chapters.append({"title": title, "url": url})
    return chapters


def _merge_book_info(base: Dict[str, str], extra: Dict[str, str]) -> Dict[str, str]:
    merged = dict(base)
    for key, value in extra.items():
        if value and (not merged.get(key) or merged.get(key) == "Unknown"):
            merged[key] = value
    return merged


def _chapter_referer(url: str) -> str:
    parsed = urlparse(url)
    match = re.match(r"(?P<book_dir>/read/[^/]+)/[^/]+\.html?$", parsed.path, flags=re.I)
    if match:
        return f"{parsed.scheme or 'https'}://{parsed.netloc}{match.group('book_dir')}/"
    return BASE_URL


def _find_catalog_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> str:
    node = soup.select_one("a[rel='index'][href], #info_url[href]")
    if node and node.get("href"):
        return _absolute_url(chapter_url, node["href"])
    for a in soup.select("a[href]"):
        text = _clean_spaces(_text(a))
        if text in {"目录", "章节目录", "返回目录"} or "目录" in text:
            href = _absolute_url(chapter_url, a.get("href", ""))
            if "/index/" in href:
                return href
    return ""


def getText(url: str = DEFAULT_URL) -> Dict:
    url = _ensure_url(url)
    soup = _fetch_html(url)
    original_url = url

    if _is_chapter_url(url, url):
        catalog_url = _find_catalog_url_from_chapter(soup, url)
        if catalog_url:
            url = catalog_url
            soup = _fetch_html(url, referer=original_url)

    book_info = _get_book_info(soup, url)
    book_key = _book_key_from_url(url) or _book_key_from_url(book_info.get("catalog_url", "")) or _book_key_from_url(book_info.get("book_url", ""))

    book_page_url = book_info.get("book_url", "")
    if book_page_url and not _looks_like_local_file(url) and not re.search(r"/book/", urlparse(url).path):
        try:
            detail_soup = _fetch_html(book_page_url, referer=url)
            book_info = _merge_book_info(book_info, _get_book_info(detail_soup, book_page_url))
        except Exception:
            pass

    catalog_urls = _expand_catalog_urls(soup, url)
    if not catalog_urls and book_info.get("catalog_url"):
        catalog_url = book_info["catalog_url"]
        try:
            catalog_soup = _fetch_html(catalog_url, referer=url)
            catalog_urls = _expand_catalog_urls(catalog_soup, catalog_url) or [catalog_url]
            if not book_key:
                book_key = _book_key_from_url(catalog_url)
        except Exception:
            catalog_urls = []

    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    if catalog_urls:
        for catalog_url in catalog_urls:
            try:
                catalog_soup = soup if _normalized_url(catalog_url) == _normalized_url(url) else _fetch_html(catalog_url, referer=url)
            except Exception as exc:
                _safe_print(f"Canh bao: bo qua trang muc luc {catalog_url}: {exc}")
                continue
            for chapter in _get_list_chapters(catalog_soup, catalog_url, book_key=book_key):
                key = _normalized_url(chapter["url"])
                if key in seen:
                    continue
                seen.add(key)
                chapters.append(chapter)
            time.sleep(SLEEP_BETWEEN_PAGES)

    if not chapters:
        for chapter in _get_list_chapters(soup, url, book_key=book_key):
            key = _normalized_url(chapter["url"])
            if key in seen:
                continue
            seen.add(key)
            chapters.append(chapter)

    book_info["url"] = book_info.get("book_url") or original_url
    return {
        "title": book_info["title"],
        "author": book_info["author"],
        "status": book_info.get("status", ""),
        "category": book_info.get("category", ""),
        "update_time": book_info.get("update_time", ""),
        "latest_chapter": book_info.get("latest_chapter", ""),
        "latest_chapter_url": book_info.get("latest_chapter_url", ""),
        "chapters": chapters,
        "total_chapters": len(chapters),
        "cover_url": book_info.get("cover_url", ""),
        "intro": book_info.get("intro", ""),
        "url": book_info.get("url", original_url),
        "catalog_url": book_info.get("catalog_url", ""),
    }


def _chapter_title_from_page(soup: BeautifulSoup, book_title: str = "", fallback: str = "") -> str:
    title = _text(soup.select_one(".read .booktitle h1") or soup.select_one(".booktitle h1") or soup.find("h1"))
    if not title:
        title_tag = _text(soup.find("title"))
        parts = [part.strip() for part in re.split(r"[-_]", title_tag) if part.strip()]
        if parts:
            title = parts[0]

    title = _clean_spaces(title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title) :].strip(" -_:：")
    title = re.sub(r"\s*[-_]?.*?uu看书.*$", "", title, flags=re.I).strip(" -_:：")
    title = re.sub(r"\s*[-_]?.*?zhaoshuyuan\.net.*$", "", title, flags=re.I).strip(" -_:：")
    return title or fallback or "Chapter"


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ").replace("\u3000", " ")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    trash_patterns = re.compile(
        r"(uu看书|zhaoshuyuan\.net|69shuba|69书吧|上一章|下一章|返回目录|章节目录|目录|"
        r"加书签|加入书架|书签|报错|章节错误|无需登陆|无需登录|广告|手机阅读|电脑版|"
        r"本章未完|点击下一页|Copyright|readSet|chaptererror|posterror)",
        flags=re.I,
    )

    paragraphs: List[str] = []
    for raw_line in raw_text.split("\n"):
        line = _clean_spaces(raw_line)
        if not line:
            continue
        if title and line == title:
            continue
        if trash_patterns.search(line):
            continue
        if re.fullmatch(r"\(?https?://[^\s)]+\)?", line, flags=re.I):
            continue
        if len(line) < 2:
            continue
        paragraphs.append(line)
    return paragraphs


def _get_chapter_content_html(soup: BeautifulSoup, title: str = "") -> str:
    content = (
        soup.select_one("#chaptercontent")
        or soup.select_one(".read .content")
        or soup.select_one(".chaptercontent")
        or soup.select_one("article")
    )
    if not content:
        candidates = [
            node
            for node in soup.select(".content, .chapter-content, .reader, .read")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 120
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return "<p>(Khong co noi dung)</p>"

    content = BeautifulSoup(str(content), "html.parser")
    for node in content.find_all(["script", "style", "ins", "iframe", "select", "input"]):
        node.decompose()
    for node in content.select(".ads, .ad, .readad, .chapter-nav, .pager, .readpage, .bookvote"):
        node.decompose()
    for a in content.find_all("a"):
        a.decompose()
    for p in content.find_all("p"):
        if re.search(r"(报错|章节错误|无需登陆|无需登录|69shuba|69书吧)", _clean_spaces(p.get_text(" ", strip=True)), flags=re.I):
            p.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")

    paragraphs = _clean_chapter_lines(content.get_text("\n", strip=False), title=title)
    if not paragraphs:
        return "<p>(Khong co noi dung)</p>"
    return "\n".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)


def _retry_delay_seconds(status_code: Optional[int], attempt: int) -> float:
    if status_code == 403:
        return min(14.0, 4.0 + attempt * 2.0)
    if status_code == 429:
        return min(20.0, 5.0 * attempt)
    return max(SLEEP_BETWEEN_CHAPS, 1.5 * attempt)


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
            soup, status_code = _fetch_html_with_status(url, tries=1, referer=_chapter_referer(url))
            last_status = status_code if isinstance(status_code, int) else None
            title = _chapter_title_from_page(soup, book_title=book_title, fallback=fallback_title)
            content_html = _get_chapter_content_html(soup, title=title)
            text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
            if not text or "Khong co noi dung" in text:
                raise FetchHtmlError("No chapter content", last_status)
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


def _read_cached_chapter(html_path: Path) -> Dict[str, str]:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    title = _text(soup.find("h1")) or _text(soup.find("title")) or html_path.stem
    article = soup.select_one("article.chapter") or soup.select_one("article") or soup.find("body") or soup
    article = BeautifulSoup(str(article), "html.parser")
    for node in article.select(".source"):
        node.decompose()
    for h1 in article.find_all("h1"):
        h1.decompose()
    content_html = "\n".join(str(child) for child in article.contents).strip()
    if not content_html:
        content_html = "<p>(Khong co noi dung)</p>"
    text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
    status_code = "ERR_CACHE" if _looks_like_failed_content(text) else "CACHE"
    return {
        "title": title,
        "content_html": content_html,
        "text": text,
        "url": str(html_path),
        "status_code": status_code,
        "html_path": str(html_path),
    }


def _looks_like_failed_content(text: str) -> bool:
    text = _clean_spaces(text)
    if not text:
        return True
    return bool(re.search(r"(Khong tai duoc noi dung|Khong co noi dung|Chapter error|\(ERR\))", text, flags=re.I))


def _is_failed_chapter_data(data: Dict[str, object]) -> bool:
    text = str(data.get("text") or BeautifulSoup(str(data.get("content_html") or ""), "html.parser").get_text("\n", strip=True))
    if _looks_like_failed_content(text):
        return True
    status = data.get("status_code")
    return status not in ("CACHE", "FILE", 200)


def _write_chapter_html(data: Dict[str, str], book_dir: str | Path, idx: int, source_url: str = "") -> Path:
    html_path = _chapter_html_path(book_dir, idx, data.get("title") or f"Chapter {idx}")
    if not html_path.exists() or _looks_like_failed_content(html_path.read_text(encoding="utf-8", errors="ignore")):
        html_path.write_text(
            _chapter_html_doc(data.get("title") or f"Chapter {idx}", data.get("content_html") or "", data.get("url") or source_url),
            encoding="utf-8",
        )
    data["html_path"] = str(html_path)
    return html_path


def _save_chapter_html(
    chapter: Dict[str, str],
    idx: int,
    book_dir: str | Path,
    *,
    book_title: str = "",
    force: bool = False,
) -> Dict[str, str]:
    cached_path = _find_cached_chapter_path(book_dir, idx)
    if cached_path and not force:
        data = _read_cached_chapter(cached_path)
        if not _is_failed_chapter_data(data):
            return data

    data = fetch_chapter_content(
        chapter["url"],
        fallback_title=chapter.get("title", f"Chapter {idx}"),
        book_title=book_title,
    )
    if not _is_failed_chapter_data(data):
        _write_chapter_html(data, book_dir, idx, chapter.get("url", ""))
    elif cached_path and _is_failed_chapter_data(_read_cached_chapter(cached_path)):
        try:
            cached_path.unlink()
        except OSError:
            pass
    return data


def _normalize_range(total: int, start: int = 1, end: Optional[int] = None) -> Tuple[int, int]:
    if total <= 0:
        return 1, 0
    start = max(1, start)
    end = total if end is None else min(total, end)
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
    book_dir = Path(book_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    start, end = _normalize_range(len(chapters), start, end)
    selected_total = end - start + 1
    downloaded: List[Dict[str, str]] = []
    failures: List[Tuple[int, object]] = []

    _safe_print(f"Bat dau tai/cache {selected_total} chuong vao: {book_dir}")
    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        data = _save_chapter_html(chapter, idx, book_dir, book_title=book_info.get("title", ""), force=force)
        if _is_failed_chapter_data(data):
            failures.append((idx, data.get("status_code", "ERR")))
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
    if failures:
        sample = ", ".join(f"{idx}:{status}" for idx, status in failures[:8])
        suffix = "..." if len(failures) > 8 else ""
        _safe_print(f"Canh bao: con {len(failures)} chuong loi ({sample}{suffix}). Chay lai se thu lai cache loi.")
    return downloaded


def _selected_chapter_data(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: str | Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
) -> List[Dict[str, str]]:
    book_dir = Path(book_dir)
    start, end = _normalize_range(len(chapters), start, end)
    items: List[Dict[str, str]] = []
    failures: List[Tuple[int, object]] = []

    for idx in range(start, end + 1):
        cached = _find_cached_chapter_path(book_dir, idx)
        if cached:
            data = _read_cached_chapter(cached)
            if _is_failed_chapter_data(data):
                data = _save_chapter_html(chapters[idx - 1], idx, book_dir, book_title=book_info.get("title", ""))
        else:
            data = _save_chapter_html(chapters[idx - 1], idx, book_dir, book_title=book_info.get("title", ""))
        items.append(data)
        status = data.get("status_code")
        if status not in ("CACHE", "FILE", 200):
            failures.append((idx, status))

    if failures:
        sample = ", ".join(f"{idx}:{status}" for idx, status in failures[:5])
        suffix = "..." if len(failures) > 5 else ""
        _safe_print(f"[Epub] Canh bao: {len(failures)} chuong loi ({sample}{suffix})")

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
    start, end = _normalize_range(len(chapters), start, end)
    selected_chapters = chapters[start - 1 : end]
    chapters_data = _selected_chapter_data(book_info, chapters, book_dir, start=start, end=end)

    suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    epub_path = book_dir / f"{_safe_filename(book_info['title'])}{suffix}.epub"

    _safe_print(f"[Epub] Dang tao ebook: {epub_path}")
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
    cover_url = _get_book_info(soup, book_page_url).get("cover_url") or ""
    cover_bytes, cover_ext = _download_cover(cover_url)
    if cover_bytes and cover_ext:
        cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
        return cover_bytes, cover_ext, cover_url
    return None, None, cover_url or None


def _prepare_book_dir(book_info: Dict[str, str]) -> Path:
    book_dir = OUTPUT_BASE / _safe_filename(book_info["title"])
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
        f"Catalog: {book_info.get('catalog_url', '')}",
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
    url = _ensure_url(url)
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
        "catalog_url": data.get("catalog_url", ""),
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
    if book_info.get("intro"):
        intro = book_info["intro"]
        _safe_print(f"Gioi thieu    : {intro[:160]}{'...' if len(intro) > 160 else ''}")

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
    book_info, chapters, book_dir, cover_bytes, cover_ext = _load_book_context(args.url or DEFAULT_URL)
    if not chapters:
        raise RuntimeError("Khong tim thay chuong")
    start, end = _normalize_range(len(chapters), args.start, args.end)
    download_chapters(book_info, chapters, book_dir, start=start, end=end, force=args.force)
    if not args.no_epub:
        build_epub(book_info, chapters, book_dir, start=start, end=end, cover_bytes=cover_bytes, cover_ext=cover_ext)


def _interactive_main() -> None:
    _safe_print("Downloader zhaoshuyuan.net / uu看书")
    while True:
        raw_url = input(f"Nhap URL [{DEFAULT_URL}]: ").strip() or DEFAULT_URL
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
    parser = argparse.ArgumentParser(description="Download zhaoshuyuan.net novel chapters and build EPUB.")
    parser.add_argument("url", nargs="?", help=f"Book/catalog/chapter URL. Default: {DEFAULT_URL}")
    parser.add_argument("--start", type=int, default=1, help="Start chapter index")
    parser.add_argument("--end", type=int, default=None, help="End chapter index")
    parser.add_argument("--force", action="store_true", help="Refetch even when cache exists")
    parser.add_argument("--no-epub", action="store_true", help="Only download/cache HTML")
    parser.add_argument("-y", "--yes", action="store_true", help="Run non-interactively")
    args = parser.parse_args(argv)

    if args.yes or args.url:
        _run_once(args)
    else:
        _interactive_main()


if __name__ == "__main__":
    main()
