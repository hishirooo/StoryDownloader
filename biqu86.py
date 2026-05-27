# -*- coding: utf-8 -*-
"""
Downloader for https://www.biqu86.com/ (新笔趣阁).

Book page sample:
  https://www.biqu86.com/118307/

Chapter page sample:
  https://www.biqu86.com/118307/52736235.html
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


BASE_URL = "https://www.biqu86.com/"
DEFAULT_URL = "https://www.biqu86.com/118307/"
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
        url = "http://" + url.lstrip("/")
    return url


def _absolute_url(page_url: str, href: str) -> str:
    href = html.unescape(href or "").strip()
    if not href:
        return page_url
    if href.startswith("//"):
        scheme = urlparse(page_url).scheme or "http"
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


def _field_text(soup: BeautifulSoup, label: str) -> str:
    for node in soup.select(".info p, .fix p, .top p, #info p, #maininfo p, .wppc p"):
        text = _clean_spaces(node.get_text(" ", strip=True))
        if text.startswith(label):
            return _strip_label(text, label)
    return ""


def _get_book_info(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, str]:
    title = (
        _meta_content(soup, "og:novel:book_name", "og:title")
        or _text(soup.select_one(".info .top h1"))
        or _text(soup.select_one(".info h1"))
        or _text(soup.select_one("#info h1"))
        or _text(soup.find("h1"))
    )
    if not title:
        title_tag = _text(soup.find("title"))
        title = re.split(r"_|\-|最新章节|TXT下载|新笔趣阁|笔趣阁", title_tag, maxsplit=1)[0].strip()

    author = _meta_content(soup, "og:novel:author") or _field_text(soup, "作者")
    category = _meta_content(soup, "og:novel:category") or _field_text(soup, "类别")
    if not category:
        links = soup.select(".con_top a")
        if len(links) >= 2:
            category = _text(links[1])

    status = _meta_content(soup, "og:novel:status") or _field_text(soup, "状态")
    update_time = _meta_content(soup, "og:novel:update_time") or _field_text(soup, "更新") or _field_text(soup, "最后更新")
    latest_chapter = (
        _meta_content(soup, "og:novel:lastest_chapter_name", "og:novel:latest_chapter_name")
        or _text(soup.select_one(".info a[href$='.html']"))
        or _text(soup.select_one(".section-list a[href$='.html']"))
        or _text(soup.select_one("#info a[rel='chapter']"))
    )
    latest_url = _meta_content(soup, "og:novel:lastest_chapter_url", "og:novel:latest_chapter_url")
    latest_node = soup.select_one(".info a[href$='.html']") or soup.select_one(".section-list a[href$='.html']") or soup.select_one("#info a[rel='chapter']")
    if not latest_url and latest_node and latest_node.get("href"):
        latest_url = latest_node["href"]
    if latest_url:
        latest_url = _absolute_url(page_url, latest_url)

    cover_url = _meta_content(soup, "og:image")
    if not cover_url:
        img = soup.select_one(".imgbox img[data-original], .imgbox img[src], #fmimg img[data-original], #fmimg img[src], img.lazy[data-original]")
        if img:
            cover_url = (img.get("data-original") or img.get("src") or "").strip()
    if cover_url:
        cover_url = _absolute_url(page_url, cover_url)

    intro = _meta_content(soup, "og:description", "description")
    intro_node = soup.select_one(".desc.m-desc") or soup.select_one(".m-desc") or soup.select_one("#intro")
    if intro_node:
        intro = intro_node.get_text("\n", strip=True)
    intro = _clean_spaces(intro)

    canonical = soup.select_one("link[rel='canonical'][href]")
    book_url = _meta_content(soup, "og:novel:url", "og:novel:read_url")
    if not book_url and canonical:
        book_url = canonical.get("href", "")
    if book_url:
        book_url = _absolute_url(page_url, book_url)

    total_chapters = 0
    page_text = _clean_spaces(soup.get_text(" ", strip=True))
    option_max = 0
    for option in soup.select("select#indexselect option"):
        option_text = _clean_spaces(option.get_text(" ", strip=True))
        option_match = re.search(r"-\s*(\d+)\s*章", option_text)
        if option_match:
            try:
                option_max = max(option_max, int(option_match.group(1)))
            except ValueError:
                pass
    match = re.search(r"共\s*(\d+)\s*章节", page_text) or re.search(r"共\s*(\d+)\s*章", page_text)
    if match:
        try:
            total_chapters = int(match.group(1))
        except ValueError:
            total_chapters = 0
    total_chapters = max(total_chapters, option_max)

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
        "total_chapters": total_chapters,
        "url": book_url or page_url,
    }


def _book_dir_from_url(url: str) -> str:
    path = urlparse(url).path
    match = re.match(r"^/(?P<book_id>\d+)/(?:\d+/)?$", path, flags=re.I)
    if match:
        return f"/{match.group('book_id')}/"
    if path.endswith("/"):
        return path
    return path.rsplit("/", 1)[0] + "/"


def _chapter_id_from_url(url: str) -> Optional[str]:
    match = re.search(r"/(?P<id>\d+)(?:_\d+)?\.html?$", urlparse(url).path, flags=re.I)
    return match.group("id") if match else None


def _is_chapter_url(page_url: str, chapter_url: str, *, allow_part: bool = False) -> bool:
    page = urlparse(page_url if urlparse(page_url).scheme else BASE_URL)
    target = urlparse(chapter_url)
    if target.netloc and page.netloc and target.netloc != page.netloc:
        return False
    book_dir = _book_dir_from_url(page_url if urlparse(page_url).scheme else DEFAULT_URL)
    if not target.path.startswith(book_dir):
        return False
    basename = target.path.rsplit("/", 1)[-1]
    pattern = r"\d+(?:_\d+)?\.html?" if allow_part else r"\d+\.html?"
    return re.fullmatch(pattern, basename, flags=re.I) is not None

def _catalog_page_number(url: str) -> int:
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"/\d+/(\d+)$", path, flags=re.I)
    return int(match.group(1)) if match else 1

def _is_catalog_url(url: str) -> bool:
    return bool(re.fullmatch(r"/\d+(?:/\d+)?/?", urlparse(url).path, flags=re.I))

def _get_catalog_urls(soup: BeautifulSoup, page_url: str) -> List[str]:
    urls: List[str] = []
    if _is_catalog_url(page_url):
        urls.append(page_url)
    for option in soup.select("select#indexselect option[value]"):
        urls.append(_absolute_url(page_url, option.get("value", "")))
    for a in soup.select(".index-container a[href]"):
        urls.append(_absolute_url(page_url, a.get("href", "")))

    seen: set[str] = set()
    result: List[str] = []
    for url in urls:
        key = _normalized_url(url)
        if key in seen or not _is_catalog_url(url):
            continue
        seen.add(key)
        result.append(url)
    return sorted(result, key=_catalog_page_number)

def _expand_catalog_urls(soup: BeautifulSoup, page_url: str) -> List[str]:
    catalog_urls = _get_catalog_urls(soup, page_url)
    if not catalog_urls:
        return []
    if len(catalog_urls) > 1 and soup.select_one("select#indexselect option[value]"):
        return catalog_urls

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

    return sorted(expanded, key=_catalog_page_number)

def _chapter_containers(soup: BeautifulSoup) -> List:
    containers = []
    for h2 in soup.select("h2.layout-tit"):
        if "正文" not in _clean_spaces(h2.get_text(" ", strip=True)):
            continue
        box = h2.find_next_sibling("div", class_="section-box")
        if box:
            containers.extend(box.select("ul.section-list"))
    if not containers:
        containers = soup.select("ul.section-list")
    if not containers:
        list_node = soup.select_one("#list")
        if list_node:
            containers = [list_node]
    return containers


def _extract_chapters_from_catalog(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    for container in _chapter_containers(soup):
        for a in container.select("a[href]"):
            title = _clean_spaces(a.get("title") or _text(a))
            url = _absolute_url(page_url, a.get("href", ""))
            if not title or not _is_chapter_url(page_url, url):
                continue
            key = f"{title}\n{_normalized_url(url)}"
            if key in seen:
                continue
            seen.add(key)
            chapters.append({"title": title, "url": url})
    return chapters


def _find_catalog_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> Optional[str]:
    for selector in ("#info_url[href]", "a[rel='index'][href]", ".bottem1 a[href]", ".bottem2 a[href]"):
        for a in soup.select(selector):
            text = _clean_spaces(_text(a))
            if "目录" in text:
                return _absolute_url(chapter_url, a.get("href", ""))
    return None


def getText(url: str = DEFAULT_URL) -> Dict:
    url = _ensure_url(url)
    local_input = _looks_like_local_file(url)
    soup = _fetch_html(url)
    original_url = url

    if not local_input and _is_chapter_url(url, url, allow_part=True):
        catalog_url = _find_catalog_url_from_chapter(soup, url)
        if catalog_url and catalog_url != url:
            url = catalog_url
            soup = _fetch_html(url, referer=original_url)

    info = _get_book_info(soup, url)
    if local_input:
        chapters = _extract_chapters_from_catalog(soup, info.get("url") or BASE_URL)
    else:
        catalog_urls = _expand_catalog_urls(soup, url)
        if not catalog_urls and info.get("url"):
            catalog_urls = [info["url"]]

        chapters = []
        for catalog_url in catalog_urls:
            try:
                catalog_soup = soup if _normalized_url(catalog_url) == _normalized_url(url) else _fetch_html(catalog_url, referer=url)
            except Exception as exc:
                _safe_print(f"Canh bao: bo qua trang muc luc {catalog_url}: {exc}")
                continue
            chapters.extend(_extract_chapters_from_catalog(catalog_soup, catalog_url))
            time.sleep(SLEEP_BETWEEN_PAGES)

        if not chapters:
            chapters = _extract_chapters_from_catalog(soup, info.get("url") or url)
    total = info.get("total_chapters") or len(chapters)

    return {
        "title": info["title"],
        "author": info["author"],
        "status": info.get("status", ""),
        "category": info.get("category", ""),
        "update_time": info.get("update_time", ""),
        "latest_chapter": info.get("latest_chapter", ""),
        "latest_chapter_url": info.get("latest_chapter_url", ""),
        "chapters": chapters,
        "total_chapters": total,
        "cover_url": info["cover_url"],
        "intro": info["intro"],
        "url": info.get("url", url),
    }


def _chapter_title_from_page(soup: BeautifulSoup, book_title: str = "", fallback: str = "") -> str:
    title = _text(soup.select_one("h1.title") or soup.select_one("h1.bookname") or soup.find("h1"))
    if not title:
        title_tag = _text(soup.find("title"))
        match = re.search(r"_(.*?)章节", title_tag)
        if match:
            title = match.group(1)
        else:
            parts = [part.strip() for part in re.split(r"[_-]", title_tag) if part.strip()]
            title = parts[1] if len(parts) > 1 else (parts[0] if parts else "")

    title = _clean_spaces(title)
    title = re.sub(r"\s*[（(]\s*\d+\s*/\s*\d+\s*[）)]\s*$", "", title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title) :].strip(" -_:：")
    title = re.sub(r"\s*[-_]?.*?新笔趣阁.*$", "", title, flags=re.I).strip(" -_:：")
    return title or fallback or "Chapter"


def _chapter_page_count(soup: BeautifulSoup) -> int:
    text = _text(soup.select_one("h1.title") or soup.select_one("h1.bookname") or soup.find("h1"))
    match = re.search(r"[（(]\s*\d+\s*/\s*(\d+)\s*[）)]", text)
    if match:
        try:
            return max(1, int(match.group(1)))
        except ValueError:
            return 1
    return 1


def _chapter_part_urls(url: str, total_pages: int) -> List[str]:
    if total_pages <= 1:
        return [url]
    base_url = url.split("#", 1)[0].split("?", 1)[0]
    if "/" not in base_url:
        return [url]
    directory, filename = base_url.rsplit("/", 1)
    match = re.match(r"(?P<chapter_id>\d+)(?:_\d+)?\.html?$", filename, flags=re.I)
    if not match:
        return [url]
    chapter_id = match.group("chapter_id")
    return [
        f"{directory}/{chapter_id}.html" if page_no == 1 else f"{directory}/{chapter_id}_{page_no}.html"
        for page_no in range(1, total_pages + 1)
    ]


def _next_part_url(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    node = soup.select_one("#next_url[href]")
    if not node:
        return None
    text = _clean_spaces(_text(node))
    href = node.get("href", "")
    if not href:
        return None
    next_url = _absolute_url(current_url, href)
    current_id = _chapter_id_from_url(current_url)
    next_id = _chapter_id_from_url(next_url)
    if current_id and next_id and current_id == next_id and next_url != current_url:
        return next_url
    if "下一页" in text and next_url != current_url:
        return next_url
    return None


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ").replace("\u3000", " ")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    trash_patterns = re.compile(
        r"(新笔趣阁|biqu86\.com|上一章|下一章|下一页|书页/目录|目录|章节目录|加入书签|加入书架|书架|"
        r"本章未完|点击下一页继续阅读|点击切换|繁体版|简体版|广告|报错|本站|搜索引擎|readSet|list\(|zh_tran|gotop|footer)",
        flags=re.I,
    )

    paragraphs: List[str] = []
    for raw_line in raw_text.split("\n"):
        line = _clean_spaces(raw_line)
        if not line:
            continue
        if title and line == title:
            continue
        if re.fullmatch(r"[（(]?\s*\d+\s*/\s*\d+\s*[）)]?", line):
            continue
        if trash_patterns.search(line):
            continue
        if re.fullmatch(r"\(?https?://[^\s)]+\)?", line, flags=re.I):
            continue
        if len(line) < 2:
            continue
        paragraphs.append(line)
    return paragraphs


def _extract_chapter_paragraphs(soup: BeautifulSoup, title: str = "") -> List[str]:
    content = soup.select_one("#booktxt") or soup.select_one("#chaptercontent") or soup.select_one(".content")
    if not content:
        candidates = [
            node
            for node in soup.select(".content, .chapter-content, .reader, article")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 120
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return []

    content = BeautifulSoup(str(content), "html.parser")
    for node in content.find_all(["script", "style", "ins", "iframe", "select", "input"]):
        node.decompose()
    for node in content.select(".ads, .ad, .readad, .bottem1, .bottem2, #xxrsox4umt"):
        node.decompose()
    for a in content.find_all("a"):
        a.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")

    return _clean_chapter_lines(content.get_text("\n", strip=False), title=title)


def _chapter_content_html_from_pages(pages: List[BeautifulSoup], title: str = "") -> str:
    paragraphs: List[str] = []
    for page in pages:
        paragraphs.extend(_extract_chapter_paragraphs(page, title=title))
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
            local_input = _looks_like_local_file(url)
            first_soup, status_code = _fetch_html_with_status(url, tries=1, referer=_book_dir_from_url(url))
            last_status = status_code if isinstance(status_code, int) else None
            title = _chapter_title_from_page(first_soup, book_title=book_title, fallback=fallback_title)

            pages = [first_soup]
            visited = {_normalized_url(url)}
            page_urls = _chapter_part_urls(url, _chapter_page_count(first_soup))
            for page_url in page_urls[1:]:
                if local_input and not _looks_like_local_file(page_url):
                    continue
                key = _normalized_url(page_url)
                if key in visited:
                    continue
                visited.add(key)
                time.sleep(SLEEP_BETWEEN_PAGES)
                pages.append(_fetch_html(page_url, referer=url))

            current_soup = pages[-1]
            current_url = page_urls[-1] if page_urls else url
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
                current_soup = _fetch_html(next_url, referer=current_url)
                current_url = next_url
                pages.append(current_soup)

            content_html = _chapter_content_html_from_pages(pages, title=title)
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
    text = _clean_spaces(text)
    if not text:
        return True
    return bool(re.search(r"(Khong tai duoc noi dung|Khong co noi dung|Chapter error|\(ERR\))", text, flags=re.I))


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
    _safe_print("Downloader biqu86.com / 新笔趣阁")
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
    parser = argparse.ArgumentParser(description="Download biqu86.com novel chapters and build EPUB.")
    parser.add_argument("url", nargs="?", help=f"Book/chapter URL. Default: {DEFAULT_URL}")
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



try:
    from download_policy import install_adapter_policy as _install_adapter_policy
    _install_adapter_policy(globals())
except Exception:
    pass
if __name__ == "__main__":
    main()
