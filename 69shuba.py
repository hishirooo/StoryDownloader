# -*- coding: utf-8 -*-
"""
Downloader cho https://www.69shuba.com/.

Trang info mẫu:
  https://www.69shuba.com/book/48273.htm

Trang mục lục đầy đủ:
  https://www.69shuba.com/book/48273/

Trang chương mẫu:
  https://www.69shuba.com/txt/48273/37647935
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import atexit
import html
import io
import os
import re
import subprocess
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


BASE_URL = "https://www.69shuba.com/"
DEFAULT_URL = "https://www.69shuba.com/book/48273.htm"
OUTPUT_BASE = Path("output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8,zh;q=0.7,pt-BR;q=0.6,pt;q=0.5",
    "Cache-Control": "no-cache",
    "Cookie": "zh_choose=s; shuba=11129-4077-19962-2178",
    "Pragma": "no-cache",
    "Priority": "u=0, i",
    "Referer": BASE_URL,
    "Sec-CH-UA": '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

COOKIES = {
    "zh_choose": "s",
    "shuba": "11129-4077-19962-2178",
}

TIMEOUT = 25
SLEEP_BETWEEN_CHAPS = 0.9
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)

_PW = None
_BROWSER_CONTEXT = None


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


def _safe_filename(name: str, max_length: int = 150) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', " - ", name or "book")
    safe = re.sub(r"\s+", " ", safe).strip().strip(".")
    return (safe[:max_length].strip() or "book")


def slugify_vi(value: str) -> str:
    return _safe_filename(value)


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
        return page_url
    if href.startswith("//"):
        return f"{urlparse(page_url).scheme or 'https'}:{href}"
    return urljoin(page_url, href)


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_spaces(tag["content"])
    return ""


def _http_get(url: str, referer: Optional[str] = None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    kwargs = {"headers": headers, "cookies": COOKIES, "timeout": TIMEOUT}
    if USE_CURL_CFFI:
        kwargs["impersonate"] = "chrome120"
    try:
        return http_requests.get(url, **kwargs)
    except TypeError as exc:
        if USE_CURL_CFFI and "impersonate" in str(exc).lower():
            kwargs.pop("impersonate", None)
            return http_requests.get(url, **kwargs)
        if USE_CURL_CFFI and "chrome120" in str(exc).lower():
            kwargs["impersonate"] = "chrome110"
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


class FetchHtmlError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class CurlResponse:
    def __init__(self, content: bytes, status_code: int):
        self.content = content
        self.status_code = status_code
        self.headers: Dict[str, str] = {}
        self.encoding = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise FetchHtmlError(f"curl HTTP={self.status_code}", self.status_code)


def _browser_executable_path() -> Optional[str]:
    candidates = [
        os.environ.get("SHUBA_BROWSER"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _close_browser_context() -> None:
    global _PW, _BROWSER_CONTEXT
    try:
        if _BROWSER_CONTEXT is not None:
            _BROWSER_CONTEXT.close()
    except Exception:
        pass
    try:
        if _PW is not None:
            _PW.stop()
    except Exception:
        pass
    _BROWSER_CONTEXT = None
    _PW = None


atexit.register(_close_browser_context)


def _get_browser_context():
    global _PW, _BROWSER_CONTEXT
    if _BROWSER_CONTEXT is not None:
        return _BROWSER_CONTEXT

    from playwright.sync_api import sync_playwright

    executable_path = _browser_executable_path()
    if not executable_path:
        raise RuntimeError("Không tìm thấy Chrome/Edge để fallback bằng browser")

    _PW = sync_playwright().start()
    profile_dir = Path("playwright_profile") / "69shuba"
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = os.environ.get("SHUBA_BROWSER_HEADLESS", "1") != "0"
    _BROWSER_CONTEXT = _PW.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        executable_path=executable_path,
        headless=headless,
        locale="zh-CN",
        user_agent=HEADERS["User-Agent"],
        extra_http_headers={
            "Accept-Language": HEADERS["Accept-Language"],
            "Upgrade-Insecure-Requests": "1",
        },
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--disable-infobars",
        ],
    )
    _BROWSER_CONTEXT.add_cookies([
        {"name": "zh_choose", "value": COOKIES["zh_choose"], "domain": ".69shuba.com", "path": "/"},
        {"name": "shuba", "value": COOKIES["shuba"], "domain": ".69shuba.com", "path": "/"},
    ])
    return _BROWSER_CONTEXT


def _browser_get(url: str, referer: Optional[str] = None) -> CurlResponse:
    context = _get_browser_context()
    page = context.new_page()
    try:
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=TIMEOUT * 1000,
            referer=referer or BASE_URL,
        )
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        status_code = response.status if response else 0
        content = page.content().encode("utf-8", errors="replace")
        return CurlResponse(content, status_code)
    finally:
        try:
            page.close()
        except Exception:
            pass


def _curl_get(url: str, referer: Optional[str] = None) -> CurlResponse:
    marker = b"\n__HTTP_STATUS__:"
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    command = [
        "curl",
        "-L",
        "--silent",
        "--show-error",
        "--compressed",
        "--max-time",
        str(TIMEOUT),
    ]
    for key, value in headers.items():
        command.extend(["-H", f"{key}: {value}"])
    command.extend(["--cookie", "zh_choose=s; shuba=11129-4077-19962-2178"])
    command.extend(["-w", marker.decode("ascii") + "%{http_code}", url])

    output = subprocess.check_output(command, stderr=subprocess.STDOUT)
    if marker in output:
        body, status_raw = output.rsplit(marker, 1)
        match = re.search(rb"(\d{3})", status_raw)
        status_code = int(match.group(1)) if match else 0
    else:
        body = output
        status_code = 200
    return CurlResponse(body, status_code)


def _fetch_html_with_status(
    url: str,
    tries: int = 3,
    backoff: float = 0.8,
    *,
    referer: Optional[str] = None,
) -> Tuple[BeautifulSoup, int]:
    last_error: Optional[Exception] = None
    last_status: Optional[int] = None

    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(backoff * attempt)
        try:
            response = _http_get(url, referer=referer)
            status_code = getattr(response, "status_code", 200)
            last_status = status_code
            if status_code == 403 and not USE_CURL_CFFI:
                try:
                    response = _curl_get(url, referer=referer)
                    status_code = response.status_code
                except Exception:
                    response = _browser_get(url, referer=referer)
                    status_code = response.status_code
                if status_code == 403:
                    response = _browser_get(url, referer=referer)
                    status_code = response.status_code
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

    raise FetchHtmlError(f"Không tải được HTML: {url} ({last_error})", last_status)


def _fetch_html(url: str, tries: int = 3, backoff: float = 0.8, *, referer: Optional[str] = None) -> BeautifulSoup:
    soup, _ = _fetch_html_with_status(url, tries=tries, backoff=backoff, referer=referer)
    return soup


def _book_id_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path
    for pattern in (r"/book/(\d+)(?:\.htm|/)?$", r"/book/(\d+)/\d+", r"/txt/(\d+)/"):
        match = re.search(pattern, path, flags=re.I)
        if match:
            return match.group(1)
    return None


def _catalog_url_from_book_id(book_id: str) -> str:
    return f"{BASE_URL}book/{book_id}/"


def _info_url_from_book_id(book_id: str) -> str:
    return f"{BASE_URL}book/{book_id}.htm"


def _chapter_referer(url: str) -> str:
    book_id = _book_id_from_url(url)
    return _catalog_url_from_book_id(book_id) if book_id else BASE_URL


def _get_book_info(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, str]:
    title = (
        _meta_content(soup, "og:novel:book_name", "og:title")
        or _text(soup.select_one(".booknav2 h1, .bookinfo h1, .book h1, h1"))
    )
    if title:
        title = re.split(r"无弹窗|最新章节|txt全集下载|-69书吧", title, maxsplit=1)[0].strip(" -_,，")

    page_text = soup.get_text("\n", strip=True)

    author = _meta_content(soup, "og:novel:author")
    if not author:
        match = re.search(r"作者\s*[:：]\s*([^\n]+)", page_text)
        if match:
            author = _clean_spaces(match.group(1))

    category = _meta_content(soup, "og:novel:category")
    if not category:
        match = re.search(r"分类\s*[:：]\s*([^\n]+)", page_text)
        if match:
            category = _clean_spaces(match.group(1))

    status = _meta_content(soup, "og:novel:status")
    if not status:
        match = re.search(r"(连载|連載|全本|完结|完結)", page_text)
        if match:
            status = match.group(1)

    update_time = _meta_content(soup, "og:novel:update_time")
    if not update_time:
        match = re.search(r"更新\s*[:：]\s*([0-9-]{8,10})", page_text)
        if match:
            update_time = match.group(1)

    latest_chapter = _meta_content(soup, "og:novel:latest_chapter_name")
    latest_url = _meta_content(soup, "og:novel:latest_chapter_url")
    if latest_url:
        latest_url = _absolute_url(page_url, latest_url)

    intro = _meta_content(soup, "og:description", "description")
    intro_node = soup.select_one("#intro, .intro, .bookintro, .intro_txt, .book_intro, .summary")
    if intro_node:
        intro = _clean_spaces(intro_node.get_text("\n", strip=True))
    elif "小说关键词" in page_text:
        before_keyword = page_text.split("小说关键词", 1)[0]
        marker_match = re.search(r"(?:章节数|完整目录)\s*\n?(.*)$", before_keyword, flags=re.S)
        if marker_match:
            candidate = _clean_spaces(marker_match.group(1))
            if len(candidate) > 30:
                intro = candidate

    cover_url = _meta_content(soup, "og:image")
    if not cover_url:
        img = soup.select_one(".imgbox img[src], .bookimg img[src], .book-cover img[src], img[src]")
        if img:
            src = img.get("src", "").strip()
            if src and not src.startswith("data:"):
                cover_url = src
    if cover_url:
        cover_url = _absolute_url(page_url, cover_url)

    return {
        "title": title or "Unknown",
        "author": author or "Unknown",
        "status": status or "",
        "category": category or "",
        "update_time": update_time or "",
        "latest_chapter": latest_chapter or "",
        "latest_chapter_url": latest_url or "",
        "cover_url": cover_url or "",
        "intro": intro or "",
        "url": page_url,
    }


def _is_chapter_url(page_url: str, chapter_url: str) -> bool:
    target = urlparse(chapter_url)
    if target.netloc and target.netloc != urlparse(page_url).netloc:
        return False
    book_id = _book_id_from_url(page_url)
    target_book_id = _book_id_from_url(chapter_url)
    if book_id and target_book_id and book_id != target_book_id:
        return False
    return re.fullmatch(r"/(?:txt|book)/\d+/\d+(?:\.html?)?/?", target.path, flags=re.I) is not None


def _chapter_url_id(chapter_url: str) -> int:
    match = re.search(r"/(\d+)/?$", urlparse(chapter_url).path)
    return int(match.group(1)) if match else 0


def _chapter_number(title: str) -> Optional[int]:
    match = re.search(r"第\s*(\d+)\s*章", title or "")
    return int(match.group(1)) if match else None


def _chapter_url_key(chapter_url: str) -> str:
    path = urlparse(chapter_url or "").path
    path = re.sub(r"\.html?$", "", path, flags=re.I)
    return path.rstrip("/")


def _same_chapter_url(left: str, right: str) -> bool:
    left_key = _chapter_url_key(left)
    right_key = _chapter_url_key(right)
    return bool(left_key and right_key and left_key == right_key)


def _is_likely_catalog_noise(title: str) -> bool:
    if _chapter_number(title) is not None:
        return False
    return re.search(
        r"(请假|休息|感言|求月票|月票|新书|生日快乐|中秋快乐|新年快乐|高考|"
        r"加油|声明|通知|公告|更新|提前发|已上传|开始上传|完本)",
        title or "",
    ) is not None


def _filter_chapter_catalog(chapters: List[Dict[str, str]], info: Dict[str, str]) -> List[Dict[str, str]]:
    filtered = list(chapters)
    if not filtered:
        return filtered

    latest_title = info.get("latest_chapter", "") or ""
    latest_url = info.get("latest_chapter_url", "") or ""

    trimmed_to_latest = False
    if latest_url and not _is_likely_catalog_noise(latest_title):
        for idx, chapter in enumerate(filtered):
            if _same_chapter_url(chapter.get("url", ""), latest_url):
                filtered = filtered[:idx + 1]
                trimmed_to_latest = True
                break

    if not trimmed_to_latest:
        latest_number = _chapter_number(latest_title)
        if latest_number is not None:
            for idx in range(len(filtered) - 1, -1, -1):
                if _chapter_number(filtered[idx].get("title", "")) == latest_number:
                    filtered = filtered[:idx + 1]
                    break

    numbered_indexes = [
        idx for idx, chapter in enumerate(filtered)
        if _chapter_number(chapter.get("title", "")) is not None
    ]
    if len(numbered_indexes) < 10:
        return filtered

    last_numbered_idx = numbered_indexes[-1]
    last_numbered_url_id = _chapter_url_id(filtered[last_numbered_idx].get("url", ""))
    kept_tail: List[Dict[str, str]] = []
    for chapter in filtered[last_numbered_idx + 1:]:
        title = chapter.get("title", "")
        url_id = _chapter_url_id(chapter.get("url", ""))
        if _is_likely_catalog_noise(title):
            continue
        if url_id and last_numbered_url_id and url_id <= last_numbered_url_id:
            continue
        kept_tail.append(chapter)

    return filtered[:last_numbered_idx + 1] + kept_tail


def _get_list_chapters(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()

    roots = soup.select("#catalog, .catalog, .chapter-list, .list, .listmain, .mulu, .booklist")
    main_node = soup.select_one("main")
    if main_node is not None:
        roots.append(main_node)
    if not roots:
        roots = [soup]

    for root in roots:
        for a in root.find_all("a", href=True):
            title = _clean_spaces(_text(a))
            url = _absolute_url(page_url, a.get("href", ""))
            if not title or not _is_chapter_url(page_url, url):
                continue
            if url in seen:
                continue
            seen.add(url)
            chapters.append({"title": title, "url": url})

    if chapters:
        chapters.sort(key=lambda item: (_chapter_number(item["title"]) is None, _chapter_number(item["title"]) or 10**9, _chapter_url_id(item["url"])))
        if all(_chapter_number(item["title"]) is None for item in chapters):
            chapters.sort(key=lambda item: _chapter_url_id(item["url"]))
    return chapters


def _find_catalog_url(soup: BeautifulSoup, page_url: str) -> Optional[str]:
    for a in soup.find_all("a", href=True):
        text = _clean_spaces(_text(a))
        if text in {"完整目录", "目录", "章节列表"} or "完整目录" in text:
            url = _absolute_url(page_url, a["href"])
            if _book_id_from_url(url):
                return url

    book_id = _book_id_from_url(page_url)
    if book_id:
        return _catalog_url_from_book_id(book_id)
    return None


def getText(url: str) -> Dict:
    url = _ensure_url(url)
    try:
        soup = _fetch_html(url)
    except FetchHtmlError:
        book_id = _book_id_from_url(url)
        if not book_id:
            raise
        catalog_url = _catalog_url_from_book_id(book_id)
        soup = _fetch_html(catalog_url, referer=_info_url_from_book_id(book_id))
        url = catalog_url
    info = _get_book_info(soup, url)

    catalog_url = _find_catalog_url(soup, url)
    chapters = _get_list_chapters(soup, url)
    if catalog_url and catalog_url != url:
        catalog_soup = _fetch_html(catalog_url, referer=url)
        catalog_chapters = _get_list_chapters(catalog_soup, catalog_url)
        if len(catalog_chapters) >= len(chapters):
            chapters = catalog_chapters
        if info["title"] == "Unknown":
            info = _get_book_info(catalog_soup, catalog_url)
            info["url"] = url
    elif not chapters and catalog_url:
        catalog_soup = _fetch_html(catalog_url, referer=url)
        chapters = _get_list_chapters(catalog_soup, catalog_url)

    raw_total_chapters = len(chapters)
    chapters = _filter_chapter_catalog(chapters, info)

    return {
        "title": info["title"],
        "author": info["author"],
        "status": info.get("status", ""),
        "category": info.get("category", ""),
        "update_time": info.get("update_time", ""),
        "latest_chapter": info.get("latest_chapter", ""),
        "latest_chapter_url": info.get("latest_chapter_url", ""),
        "chapters": chapters,
        "total_chapters": len(chapters),
        "raw_total_chapters": raw_total_chapters,
        "cover_url": info["cover_url"],
        "intro": info["intro"],
        "url": url,
    }


def _chapter_title_from_page(soup: BeautifulSoup, book_title: str = "", fallback: str = "") -> str:
    title = _text(soup.select_one("h1, .txtnav h1, .chapter-title"))
    if not title:
        title_tag = _text(soup.find("title"))
        title = re.split(r"-69书吧|_", title_tag, maxsplit=1)[0].strip()

    title = _clean_spaces(title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title):].strip(" -_:：")
    title = re.sub(r"\s*-?\s*69书吧.*$", "", title).strip(" -_:：")
    return title or fallback or "Chương"


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ").replace("\u3000", " ")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    trash_patterns = re.compile(
        r"(69书吧|69shuba\.com|上一章|下一章|返回目录|目录|书签|加入书架|推荐本书|最新网址|"
        r"手机用户|请收藏本站|无弹窗|广告|本章未完|点击下一页|章节错误|举报|Copyright)",
        flags=re.I,
    )

    lines: List[str] = []
    for raw_line in raw_text.split("\n"):
        line = _clean_spaces(raw_line)
        if not line:
            continue
        if title and line == title:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line):
            continue
        if re.fullmatch(r"作者\s*[:：].+", line):
            continue
        if trash_patterns.search(line):
            continue
        if re.fullmatch(r"\(?https?://[^\s)]+\)?", line, flags=re.I):
            continue
        if len(line) < 2:
            continue
        lines.append(line)
    return lines


def _get_chapter_content_html(soup: BeautifulSoup, title: str = "") -> str:
    candidates = [
        node for node in soup.select("#content, .txtnav, .txt, .readcotent, .readcontent, .chapter-content, .content, article")
        if len(_clean_spaces(node.get_text(" ", strip=True))) > 120
    ]
    content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        body_text = _clean_spaces(soup.get_text(" ", strip=True))
        if len(body_text) > 120:
            content = soup.find("body") or soup
    if not content:
        return "<p>(Không có nội dung)</p>"

    content = BeautifulSoup(str(content), "html.parser")
    for node in content.find_all(["script", "style", "ins", "iframe", "select", "input", "button"]):
        node.decompose()
    for node in content.select(".ad, .ads, .readad, .chapter-nav, .pager, .page, .tools, .bottom, .toplink"):
        node.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")

    paragraphs = _clean_chapter_lines(content.get_text("\n", strip=False), title=title)
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
    url = _ensure_url(url)
    last_status: Optional[int] = None
    for attempt in range(1, retries + 1):
        try:
            soup, status_code = _fetch_html_with_status(url, tries=1, referer=_chapter_referer(url))
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
                time.sleep(1.2 * attempt)
        except Exception:
            if attempt < retries:
                time.sleep(1.2 * attempt)

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


def _read_cached_chapter(html_path: Path) -> Dict[str, str]:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    title = _text(soup.find("h1")) or _text(soup.find("title")) or html_path.stem
    article = soup.select_one("article.chapter") or soup.select_one("article") or soup.find("body") or soup
    article = BeautifulSoup(str(article), "html.parser")
    for node in article.select(".source"):
        node.decompose()
    for h1 in article.find_all("h1"):
        h1.decompose()
    content_html = "\n".join(str(child) for child in article.contents).strip() or "<p>(Không có nội dung)</p>"
    text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
    return {"title": title, "content_html": content_html, "text": text, "url": str(html_path), "status_code": "CACHE"}


def _chapter_html_path(book_dir: str | Path, idx: int, title: str) -> Path:
    return Path(book_dir) / f"{idx:04d} - {_safe_filename(title, 90)}.html"


def _find_cached_chapter_path(book_dir: str | Path, idx: int) -> Optional[Path]:
    directory = Path(book_dir)
    for pattern in (f"{idx:04d} - *.html", f"{idx:04d}.html", f"chapter_{idx:04d}.html"):
        matches = sorted(directory.glob(pattern))
        if matches:
            return matches[0]
    html_dir = directory / "html"
    if html_dir.is_dir():
        for pattern in (f"{idx:04d}.html", f"{idx:04d} - *.html"):
            matches = sorted(html_dir.glob(pattern))
            if matches:
                return matches[0]
    return None


def _save_one_chapter_html(
    chapter: Dict[str, str],
    idx: int,
    out_dir: str | Path,
    *,
    book_title: str = "",
    force: bool = False,
) -> Dict[str, str]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    cached_path = _find_cached_chapter_path(out_path, idx)
    if cached_path and not force:
        return _read_cached_chapter(cached_path)

    data = fetch_chapter_content(
        chapter["url"],
        fallback_title=chapter.get("title", f"Chương {idx}"),
        book_title=book_title,
    )
    html_path = _chapter_html_path(out_path, idx, data.get("title") or chapter.get("title") or f"Chương {idx}")
    html_path.write_text(_chapter_html_doc(data["title"], data["content_html"], data["url"]), encoding="utf-8")
    data["html_path"] = str(html_path)
    return data


def _normalize_range(total: int, start: int = 1, end: Optional[int] = None) -> Tuple[int, int]:
    if total <= 0:
        return 1, 0
    start = max(1, start)
    end = total if end is None else min(total, end)
    if end < start:
        raise ValueError("Khoảng chương không hợp lệ")
    return start, end


def save_all_chapters_to_html(
    book_title: str,
    chapters: List[Dict[str, str]],
    out_dir: str,
    start: int = 1,
    end: Optional[int] = None,
    *,
    force: bool = False,
) -> List[Dict[str, str]]:
    start, end = _normalize_range(len(chapters), start, end)
    selected_total = end - start + 1
    saved: List[Dict[str, str]] = []
    _safe_print(f"Bắt đầu tải/cache {selected_total} chương vào: {out_dir}")
    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        data = _save_one_chapter_html(chapter, idx, out_dir, book_title=book_title, force=force)
        saved.append(data)
        _safe_print(chapter_log_line(done, selected_total, data.get("status_code", "ERR"), idx, len(chapters), data.get("title") or chapter.get("title") or ""))
    _safe_print(f"Hoàn tất tải/cache {selected_total} chương.")
    return saved


def _save_chapter_txt(data: Dict[str, str], txt_path: Path) -> None:
    soup = BeautifulSoup(data.get("content_html", ""), "html.parser")
    text = data.get("text") or soup.get_text("\n", strip=True)
    txt_path.write_text(f"{data.get('title', txt_path.stem)}\n\n{text}\n", encoding="utf-8")


def save_txt_from_html(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    out_dir: str | Path,
    start: int = 1,
    end: Optional[int] = None,
) -> None:
    start, end = _normalize_range(len(chapters), start, end)
    txt_dir = Path(out_dir) / "txt"
    txt_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(start, end + 1):
        cached = _find_cached_chapter_path(out_dir, idx)
        if cached:
            data = _read_cached_chapter(cached)
        else:
            data = _save_one_chapter_html(chapters[idx - 1], idx, out_dir, book_title=book_info.get("title", ""))
        _save_chapter_txt(data, txt_dir / f"{idx:04d}.txt")
    _safe_print(f"Đã lưu TXT tách chương: {txt_dir}")


def save_combined_txt(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: str | Path,
    start: int = 1,
    end: Optional[int] = None,
) -> Path:
    book_dir = Path(book_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    start, end = _normalize_range(len(chapters), start, end)
    suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    out_path = book_dir / f"{_safe_filename(book_info['title'])}{suffix}.txt"

    chunks: List[str] = [book_info["title"], f"作者：{book_info.get('author', 'Unknown')}", ""]
    if book_info.get("intro"):
        chunks.extend(["内容简介：", book_info["intro"], ""])

    for idx in range(start, end + 1):
        cached = _find_cached_chapter_path(book_dir, idx)
        if cached:
            data = _read_cached_chapter(cached)
        else:
            data = _save_one_chapter_html(chapters[idx - 1], idx, book_dir, book_title=book_info.get("title", ""))
        text = data.get("text") or BeautifulSoup(data["content_html"], "html.parser").get_text("\n", strip=True)
        chunks.extend([data["title"], "", text, ""])

    out_path.write_text("\n".join(chunks), encoding="utf-8")
    _safe_print(f"Đã lưu TXT gộp: {out_path}")
    return out_path


def _download_cover(cover_url: str) -> Tuple[Optional[bytes], Optional[str]]:
    if not cover_url or cover_url.startswith("data:"):
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
        image = Image.open(io.BytesIO(content)).convert("RGB")
        image.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88)
        return output.getvalue(), ".jpg"
    except Exception as exc:
        _safe_print(f"Không xử lý được cover bằng Pillow: {exc}")
        return content, ext


def _fetch_cover_from_book_page(book_page_url: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    book_page_url = _ensure_url(book_page_url)
    soup = _fetch_html(book_page_url)
    cover_url = _get_book_info(soup, book_page_url).get("cover_url") or ""
    cover_bytes, cover_ext = _download_cover(cover_url)
    if cover_bytes and cover_ext:
        cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
        return cover_bytes, cover_ext, cover_url
    return None, None, cover_url or None


fetch_cover_from_book_page = _fetch_cover_from_book_page


def _selected_chapter_data(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: str | Path,
    start: int = 1,
    end: Optional[int] = None,
) -> List[Dict[str, str]]:
    start, end = _normalize_range(len(chapters), start, end)
    items: List[Dict[str, str]] = []
    for idx in range(start, end + 1):
        cached = _find_cached_chapter_path(book_dir, idx)
        if cached:
            items.append(_read_cached_chapter(cached))
        else:
            items.append(_save_one_chapter_html(chapters[idx - 1], idx, book_dir, book_title=book_info.get("title", "")))
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
    selected_chapters = chapters[start - 1:end]
    chapters_data = _selected_chapter_data(book_info, chapters, book_dir, start, end)

    suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    epub_path = book_dir / f"{_safe_filename(book_info['title'])}{suffix}.epub"

    _safe_print(f"[Epub] Đang tạo ebook: {epub_path}")
    noise = io.StringIO()
    with redirect_stdout(noise):
        epub_builder.create_epub(
            book_url=book_info.get("url", ""),
            book_title=book_info.get("title", "Truyện"),
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
    _safe_print(f"[Epub] Đã tạo xong ebook: {epub_path}")
    return epub_path


def _prepare_book_context(url: str) -> Tuple[Dict[str, str], List[Dict[str, str]], Path, Optional[bytes], Optional[str]]:
    url = _ensure_url(url)
    _safe_print("Đang lấy thông tin truyện...")
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
    raw_total_chapters = data.get("raw_total_chapters", len(chapters))
    book_dir = OUTPUT_BASE / _safe_filename(book_info["title"])
    book_dir.mkdir(parents=True, exist_ok=True)
    _save_book_info(book_info, chapters, book_dir)

    _safe_print("\n-----------------Thông tin truyện-----------------")
    _safe_print(f"Tên truyện   : {book_info['title']}")
    _safe_print(f"Tác giả      : {book_info['author']}")
    if book_info.get("status"):
        _safe_print(f"Trạng thái   : {book_info['status']}")
    if book_info.get("category"):
        _safe_print(f"Thể loại     : {book_info['category']}")
    if raw_total_chapters > len(chapters):
        _safe_print(f"Số chương    : {len(chapters)} (đã lọc {raw_total_chapters - len(chapters)} mục rác)")
    else:
        _safe_print(f"Số chương    : {len(chapters)}")
    if book_info.get("latest_chapter"):
        _safe_print(f"Mới nhất     : {book_info['latest_chapter']}")
    _safe_print(f"Thư mục truyện: {book_dir}")
    _safe_print(f"EPUB sẽ lưu  : {book_dir / (_safe_filename(book_info['title']) + '.epub')}")
    if book_info.get("intro"):
        intro = book_info["intro"]
        _safe_print(f"Giới thiệu   : {intro[:160]}{'...' if len(intro) > 160 else ''}")

    cover_bytes, cover_ext = None, None
    if book_info.get("cover_url"):
        cover_bytes, cover_ext = _download_cover(book_info["cover_url"])
        if cover_bytes and cover_ext:
            cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
            cover_path = book_dir / f"cover{cover_ext}"
            cover_path.write_bytes(cover_bytes)
            _safe_print(f"Đã lưu cover: {cover_path}")

    return book_info, chapters, book_dir, cover_bytes, cover_ext


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
        "Mục lục:",
    ]
    for idx, chapter in enumerate(chapters, 1):
        lines.append(f"{idx:04d}. {chapter['title']} - {chapter['url']}")
    (book_dir / "book_info.txt").write_text("\n".join(lines), encoding="utf-8")


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
    _safe_print("Downloader 69shuba.com / 69书吧")
    while True:
        raw_url = input(f"Nhập Url [{DEFAULT_URL}]: ").strip() or DEFAULT_URL
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
                    save_all_chapters_to_html(book_info["title"], chapters, str(book_dir))
                    build_epub(book_info, chapters, book_dir, cover_bytes=cover_bytes, cover_ext=cover_ext)
                    break
                if choice == "2":
                    start = _ask_int("Chương bắt đầu: ")
                    end = _ask_int("Chương kết thúc: ", len(chapters))
                    start, end = _normalize_range(len(chapters), start, end)
                    save_all_chapters_to_html(book_info["title"], chapters, str(book_dir), start=start, end=end)
                    break
                if choice == "3":
                    idx = _ask_int("Chương cần tải: ")
                    idx, _ = _normalize_range(len(chapters), idx, idx)
                    save_all_chapters_to_html(book_info["title"], chapters, str(book_dir), start=idx, end=idx)
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
