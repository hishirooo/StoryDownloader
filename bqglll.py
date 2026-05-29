# -*- coding: utf-8 -*-
"""Downloader adapter for bqglll.cc and its bqg* API mirrors."""

from __future__ import annotations

from bs4 import BeautifulSoup
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
import argparse
import html
import io
import json
import re
import sys
import time

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


BASE_URL = "https://www.bqglll.cc/"
DEFAULT_URL = "https://www.bqglll.cc/look/104952/"
OUTPUT_BASE = Path("output")

API_HOSTS = [
    "https://www.bqg128.xyz",
    "https://www.bqg303.xyz",
    "https://m.bqg303.xyz",
    "https://www.bqg205.xyz",
    "https://www.bqg731.xyz",
    "https://www.bqg661.cc",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,vi;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": BASE_URL,
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 25
SLEEP_BETWEEN_CHAPS = 0.6
CHAPTER_RETRIES = 4
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)

_LOOK_TO_API_ID: Dict[str, str] = {}


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
        return "https://" + url.lstrip("/")
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
    return parsed._replace(fragment="", query="").geturl().rstrip("/") + (f"#{parsed.fragment}" if parsed.fragment else "")


def _http_referer(value: str) -> str:
    return value if urlparse(value or "").scheme in {"http", "https"} else BASE_URL


def _http_get(url: str, *, referer: Optional[str] = None, stream: bool = False):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    kwargs: Dict[str, Any] = {"headers": headers, "timeout": TIMEOUT, "allow_redirects": True}
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
        enc = getattr(response, attr, None) if response is not None else None
        if enc:
            candidates.append(enc)
    candidates.extend(["utf-8", "gb18030", "gbk", "big5"])
    for enc in candidates:
        if not enc:
            continue
        normalized = enc.lower().replace("_", "-")
        if normalized in {"iso-8859-1", "latin-1", "ascii"}:
            continue
        try:
            content.decode(enc)
            return enc
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
) -> Tuple[BeautifulSoup, object, str]:
    url = _ensure_url(url)
    if _looks_like_local_file(url):
        content = Path(url).read_bytes()
        return BeautifulSoup(_decode_html(content), "html.parser"), "FILE", url

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
            return BeautifulSoup(_decode_html(content, response), "html.parser"), status_code, getattr(response, "url", url)
        except Exception as exc:
            last_error = exc
            response = getattr(exc, "response", None)
            if response is not None and getattr(response, "status_code", None):
                last_status = getattr(response, "status_code")
            if attempt >= tries:
                break
    raise FetchHtmlError(f"Khong tai duoc HTML: {url} ({last_error})", last_status)


def _fetch_html(url: str, tries: int = 3, *, referer: Optional[str] = None) -> BeautifulSoup:
    soup, _status, _final_url = _fetch_html_with_status(url, tries=tries, referer=referer)
    return soup


def _host_base(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _preferred_api_hosts(preferred_url: str = "") -> List[str]:
    hosts: List[str] = []
    base = _host_base(preferred_url)
    if base and re.search(r"bqg\d+\.(?:xyz|cc)$", urlparse(base).netloc, flags=re.I):
        hosts.append(base)
    hosts.extend(API_HOSTS)
    seen: set[str] = set()
    result: List[str] = []
    for host in hosts:
        key = host.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(host.rstrip("/"))
    return result


def _api_url(host: str, endpoint: str, params: Dict[str, Any]) -> str:
    return host.rstrip("/") + endpoint + "?" + urlencode(params)


def _fetch_api_json(endpoint: str, params: Dict[str, Any], *, preferred_url: str = "") -> Tuple[Dict[str, Any], int, str]:
    last_error: Optional[Exception] = None
    last_status: Optional[int] = None
    for host in _preferred_api_hosts(preferred_url):
        url = _api_url(host, endpoint, params)
        try:
            response = _http_get(url, referer=preferred_url or BASE_URL)
            status_code = getattr(response, "status_code", 200)
            last_status = status_code
            if status_code != 200:
                continue
            text = _decode_html(getattr(response, "content", b""), response)
            data = json.loads(text)
            if isinstance(data, dict):
                return data, status_code, host
        except Exception as exc:
            last_error = exc
            continue
    raise FetchHtmlError(f"Khong goi duoc API {endpoint}: {last_error}", last_status)


def _fetch_api_book(book_id: str, *, preferred_url: str = "") -> Tuple[Dict[str, Any], int, str]:
    return _fetch_api_json("/api/book", {"id": book_id}, preferred_url=preferred_url)


def _fetch_api_booklist(book_id: str, *, preferred_url: str = "") -> Tuple[List[str], int, str]:
    data, status, host = _fetch_api_json("/api/booklist", {"id": book_id}, preferred_url=preferred_url)
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise FetchHtmlError("API booklist khong hop le", status)
    return [str(item) for item in items], status, host


def _fetch_api_chapter(book_id: str, chapter_id: str, *, preferred_url: str = "") -> Tuple[Dict[str, Any], int, str]:
    return _fetch_api_json("/api/chapter", {"id": book_id, "chapterid": chapter_id}, preferred_url=preferred_url)


def _make_hash_book_url(host: str, book_id: str) -> str:
    host = (host or API_HOSTS[0]).rstrip("/")
    return f"{host}/#/book/{book_id}/"


def _make_hash_chapter_url(host: str, book_id: str, chapter_id: int | str) -> str:
    host = (host or API_HOSTS[0]).rstrip("/")
    return f"{host}/#/book/{book_id}/{chapter_id}.html"


def _route_from_url(url: str) -> Dict[str, str]:
    parsed = urlparse(url or "")
    query = parse_qs(parsed.query or "")
    if parsed.path.rstrip("/").endswith("/api/chapter") and query.get("id") and query.get("chapterid"):
        return {"type": "api_chapter", "book_id": query["id"][0], "chapter_id": query["chapterid"][0]}
    if parsed.path.rstrip("/").endswith("/api/book") and query.get("id"):
        return {"type": "api_book", "book_id": query["id"][0]}

    for target in (parsed.fragment, parsed.path):
        match = re.fullmatch(r"/?book/(\d+)/(?:(\d+)(?:_(\d+))?\.html?)?", target or "", flags=re.I)
        if match:
            route = {"type": "hash", "book_id": match.group(1)}
            if match.group(2):
                route["chapter_id"] = match.group(2)
            if match.group(3):
                route["page"] = match.group(3)
            return route

    match = re.search(r"/look/(\d+)/(?:([0-9]+)\.html?)?", parsed.path or "", flags=re.I)
    if match:
        route = {"type": "look", "site_book_id": match.group(1)}
        if match.group(2):
            route["chapter_id"] = match.group(2)
        return route
    return {}


def _chapter_id_from_url(url: str) -> str:
    return _route_from_url(url).get("chapter_id", "")


def _book_id_from_soup(soup: BeautifulSoup, page_url: str = "") -> str:
    route = _route_from_url(page_url)
    if route.get("type") in {"hash", "api_book", "api_chapter"} and route.get("book_id"):
        return route["book_id"]

    for img in soup.select(".info .cover img[src], .info img[src], .cover img[src], img[src]"):
        src = img.get("src", "")
        for pattern in (r"/bookimg/\d+/(\d+)\.(?:jpg|jpeg|png|webp)", r"(?:^|[\\/])(\d+)\.(?:jpg|jpeg|png|webp)$"):
            match = re.search(pattern, src, flags=re.I)
            if match:
                return match.group(1)

    text = str(soup)
    for pattern in (r"[#/]book/(\d+)/(?:\d+\.html?)?",):
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1)
    return ""


def _book_url_from_any(url: str, book_id: str = "") -> str:
    route = _route_from_url(url)
    if route.get("type") == "look" and route.get("site_book_id"):
        parsed = urlparse(url)
        return parsed._replace(path=f"/look/{route['site_book_id']}/", query="", fragment="").geturl()
    if book_id:
        return _make_hash_book_url(_preferred_api_hosts(url)[0], book_id)
    return url or DEFAULT_URL


def _cover_url_from_book_id(book_id: str, host: str = BASE_URL) -> str:
    if not book_id or not str(book_id).isdigit():
        return ""
    base = _host_base(host) or BASE_URL.rstrip("/")
    return f"{base.rstrip('/')}/bookimg/{int(book_id) // 1000}/{book_id}.jpg"


def _field_between(text: str, start_label: str, end_labels: Tuple[str, ...]) -> str:
    text = _clean_spaces(text)
    pattern = re.escape(start_label) + r"\s*[:\uff1a]\s*(.*?)"
    if end_labels:
        pattern += r"(?=" + "|".join(re.escape(label) + r"\s*[:\uff1a]" for label in end_labels) + r"|$)"
    match = re.search(pattern, text)
    return _clean_spaces(match.group(1)) if match else ""


def _category_from_path(soup: BeautifulSoup) -> str:
    text = _text(soup.select_one(".path"))
    parts = [part.strip() for part in re.split(r">|›|/", text) if part.strip()]
    if len(parts) >= 2:
        value = re.sub(r"\u6700\u65b0\u7ae0\u8282|\u65e0\u5f39\u7a97", "", parts[1]).strip()
        return value
    return ""


def _get_book_info_from_soup(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, str]:
    title = _text(soup.select_one(".info h1, h1#title, h1"))
    if not title:
        title_tag = _text(soup.find("title"))
        title = re.split(r"_|-|\u6700\u65b0|\u65e0\u5f39\u7a97|\u7b14\u8da3\u9601", title_tag, maxsplit=1)[0]

    small_text = _text(soup.select_one(".info .small")) or _text(soup.select_one(".info"))
    author = _field_between(small_text, "\u4f5c\u8005", ("\u72b6\u6001", "\u66f4\u65b0", "\u66f4\u65b0\u65f6\u95f4", "\u6700\u65b0"))
    status = _field_between(small_text, "\u72b6\u6001", ("\u66f4\u65b0", "\u66f4\u65b0\u65f6\u95f4", "\u6700\u65b0"))
    update_time = _field_between(small_text, "\u66f4\u65b0\u65f6\u95f4", ("\u6700\u65b0",)) or _field_between(small_text, "\u66f4\u65b0", ("\u6700\u65b0",))
    latest_node = soup.select_one("#lastchapter a[href], .last a[href], .info .small a[href]")
    latest_chapter = _text(latest_node) or _field_between(small_text, "\u6700\u65b0", tuple())
    latest_url = _absolute_url(page_url, latest_node.get("href", "")) if latest_node else ""

    intro_node = soup.select_one("#intro") or soup.select_one(".intro dd") or soup.select_one(".intro")
    intro = _text(intro_node)
    intro = re.sub(r"^\u5185\u5bb9\u7b80\u4ecb\s*[:\uff1a]\s*", "", intro).strip()

    cover_url = ""
    img = soup.select_one(".info .cover img[src], .cover img[src], .info img[src], img[src]")
    if img:
        cover_url = _absolute_url(page_url, img.get("src", ""))

    book_id = _book_id_from_soup(soup, page_url)
    route = _route_from_url(page_url)
    if route.get("type") == "look" and route.get("site_book_id") and book_id:
        _LOOK_TO_API_ID[route["site_book_id"]] = book_id

    return {
        "title": _clean_spaces(title) or "Unknown",
        "author": _clean_spaces(author) or "Unknown",
        "status": _clean_spaces(status),
        "category": _category_from_path(soup),
        "genre": _category_from_path(soup),
        "update_time": _clean_spaces(update_time),
        "latest_chapter": _clean_spaces(latest_chapter),
        "latest_chapter_url": latest_url,
        "cover_url": cover_url,
        "intro": _clean_spaces(intro),
        "url": _book_url_from_any(page_url, book_id),
        "book_id": book_id,
        "total_chapters": 0,
    }


def _book_info_from_api(book_id: str, *, preferred_url: str = "") -> Tuple[Dict[str, str], str]:
    data, _status, host = _fetch_api_book(book_id, preferred_url=preferred_url)
    latest_id = str(data.get("lastchapterid") or "")
    latest_url = _make_hash_chapter_url(host, book_id, latest_id) if latest_id else ""
    info = {
        "title": _clean_spaces(str(data.get("title") or "Unknown")),
        "author": _clean_spaces(str(data.get("author") or "Unknown")),
        "status": _clean_spaces(str(data.get("full") or "")),
        "category": _clean_spaces(str(data.get("sortname") or "")),
        "genre": _clean_spaces(str(data.get("sortname") or "")),
        "update_time": _clean_spaces(str(data.get("lastupdate") or "")),
        "latest_chapter": _clean_spaces(str(data.get("lastchapter") or "")),
        "latest_chapter_url": latest_url,
        "cover_url": _cover_url_from_book_id(book_id, host),
        "intro": _clean_spaces(str(data.get("intro") or "")),
        "url": _make_hash_book_url(host, book_id),
        "book_id": book_id,
        "total_chapters": int(data.get("lastchapterid") or 0) if str(data.get("lastchapterid") or "").isdigit() else 0,
    }
    return info, host


def _extract_chapters_from_soup(
    soup: BeautifulSoup,
    page_url: str,
    *,
    api_book_id: str = "",
    api_host: str = "",
) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    containers = soup.select(".listmain, #list, .chapter-list, .allup")
    if not containers:
        containers = soup.select(".book") or [soup]
    for container in containers:
        for a in container.select("a[href]"):
            title = _clean_spaces(a.get("title") or _text(a))
            href = a.get("href", "")
            if not title or "\u5c55\u5f00" in title or title in {"\u5f00\u59cb\u9605\u8bfb", "\u52a0\u5165\u4e66\u67b6"} or href.startswith("javascript:"):
                continue
            url = _absolute_url(page_url, href)
            route = _route_from_url(url)
            chapter_id = route.get("chapter_id")
            if not chapter_id:
                continue
            if api_book_id and route.get("type") == "look":
                url = _make_hash_chapter_url(api_host or API_HOSTS[0], api_book_id, chapter_id)
            key = _normalized_url(url)
            if key in seen:
                continue
            seen.add(key)
            chapters.append({"title": title, "url": url})
    return chapters


def _chapters_from_api(book_id: str, *, preferred_url: str = "") -> Tuple[List[Dict[str, str]], str]:
    items, _status, host = _fetch_api_booklist(book_id, preferred_url=preferred_url)
    chapters = [
        {"title": _clean_spaces(title) or f"Chapter {idx}", "url": _make_hash_chapter_url(host, book_id, idx)}
        for idx, title in enumerate(items, 1)
    ]
    return chapters, host


def _resolve_api_book_id_from_look(url: str) -> str:
    route = _route_from_url(url)
    site_book_id = route.get("site_book_id", "")
    if not site_book_id:
        return ""
    if site_book_id in _LOOK_TO_API_ID:
        return _LOOK_TO_API_ID[site_book_id]
    parsed = urlparse(url)
    catalog_url = parsed._replace(path=f"/look/{site_book_id}/", query="", fragment="").geturl()
    soup = _fetch_html(catalog_url, referer=BASE_URL)
    api_book_id = _book_id_from_soup(soup, catalog_url)
    if api_book_id:
        _LOOK_TO_API_ID[site_book_id] = api_book_id
    return api_book_id


def getText(url: str = DEFAULT_URL) -> Dict:
    url = _ensure_url(url)
    local_input = _looks_like_local_file(url)
    route = _route_from_url(url)

    if not local_input and route.get("type") in {"hash", "api_book", "api_chapter"} and route.get("book_id"):
        book_id = route["book_id"]
        info, host = _book_info_from_api(book_id, preferred_url=url)
        chapters, host = _chapters_from_api(book_id, preferred_url=host)
        if chapters:
            info["latest_chapter"] = chapters[-1]["title"]
            info["latest_chapter_url"] = chapters[-1]["url"]
            info["total_chapters"] = len(chapters)
        return {**info, "chapters": chapters}

    if not local_input and route.get("type") == "look" and route.get("chapter_id"):
        parsed = urlparse(url)
        url = parsed._replace(path=f"/look/{route['site_book_id']}/", query="", fragment="").geturl()

    soup = _fetch_html(url)
    info = _get_book_info_from_soup(soup, url)
    book_id = info.get("book_id") or ""

    if not local_input and book_id:
        try:
            api_info, host = _book_info_from_api(book_id, preferred_url=url)
            chapters, host = _chapters_from_api(book_id, preferred_url=host)
            api_info["cover_url"] = info.get("cover_url") or api_info.get("cover_url", "")
            if route.get("type") == "look":
                api_info["url"] = url
            if chapters:
                api_info["latest_chapter"] = chapters[-1]["title"]
                api_info["latest_chapter_url"] = chapters[-1]["url"]
                api_info["total_chapters"] = len(chapters)
            return {**api_info, "chapters": chapters}
        except Exception as exc:
            _safe_print(f"Canh bao: API booklist khong dung duoc, dung HTML list: {exc}")

    chapters = _extract_chapters_from_soup(soup, url, api_book_id=book_id, api_host=_preferred_api_hosts(url)[0])
    if chapters:
        info["latest_chapter"] = chapters[-1]["title"]
        info["latest_chapter_url"] = chapters[-1]["url"]
        info["total_chapters"] = len(chapters)
    return {**info, "chapters": chapters}


def _chapter_title_from_page(soup: BeautifulSoup, fallback: str = "", book_title: str = "") -> str:
    title = _text(soup.select_one("#title") or soup.select_one(".content h1") or soup.select_one("h1"))
    if not title:
        title_tag = _text(soup.find("title"))
        title = re.split(r"_|-|\u7b14\u8da3\u9601", title_tag, maxsplit=1)[0]
    title = _clean_spaces(title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title) :].strip(" -_:\uff1a")
    return title or fallback or "Chapter"


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ").replace("\u3000", " ")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    trash = re.compile(
        r"(bqg\d+\.|bqglll\.cc|bqg78\.com|\u7b14\u8da3\u9601|\u8bf7\u6536\u85cf\u672c\u7ad9|"
        r"\u624b\u673a\u7248|\u4e0a\u4e00\u7ae0|\u4e0b\u4e00\u7ae0|\u76ee\u5f55|\u70b9\u6b64\u62a5\u9519|"
        r"\u52a0\u5165\u4e66\u7b7e|\u52a0\u5165\u4e66\u67b6|\u5b57\u4f53\uff1a|\u62a4\u773c|\u5173\u706f|"
        r"Copyright|\u5e7f\u544a)",
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
        if re.fullmatch(r"\(?https?://[^\s)]+\)?", line, flags=re.I):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return lines


def _content_html_from_lines(lines: List[str]) -> Tuple[str, str]:
    text = "\n".join(lines).strip()
    content_html = "\n".join(f"<p>{html.escape(line)}</p>" for line in lines)
    return content_html, text


def _chapter_from_api_data(data: Dict[str, Any], status_code: int, url: str, fallback_title: str = "") -> Dict[str, Any]:
    title = _clean_spaces(str(data.get("chaptername") or fallback_title or "Chapter"))
    raw_text = str(data.get("txt") or "")
    lines = _clean_chapter_lines(raw_text, title=title)
    content_html, text = _content_html_from_lines(lines)
    if not text:
        raise FetchHtmlError("No chapter content", status_code)
    return {
        "title": title,
        "content_html": content_html,
        "text": text,
        "url": url,
        "status_code": status_code,
        "parts": 1,
    }


def _extract_chapter_paragraphs(soup: BeautifulSoup, title: str = "") -> List[str]:
    content = (
        soup.select_one("#chaptercontent")
        or soup.select_one(".ReadAjax_content")
        or soup.select_one(".Readarea")
        or soup.select_one("#content")
        or soup.select_one(".content")
        or soup.select_one("article")
    )
    if not content:
        candidates = [
            node
            for node in soup.select(".book, .reader, main, body")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 120
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return []
    content = BeautifulSoup(str(content), "html.parser")
    for node in content.find_all(["script", "style", "ins", "iframe", "select", "input", "button", "form"]):
        node.decompose()
    for node in content.select(".Readpage, .readinline, .Readbtn, .ads, .ad, .path, .link, .footer"):
        node.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")
    return _clean_chapter_lines(content.get_text("\n", strip=False), title=title)


def _resolve_api_route(url: str) -> Tuple[str, str, str]:
    route = _route_from_url(url)
    if route.get("type") in {"hash", "api_chapter"} and route.get("book_id") and route.get("chapter_id"):
        return route["book_id"], route["chapter_id"], _preferred_api_hosts(url)[0]
    if route.get("type") == "look" and route.get("chapter_id"):
        api_book_id = _resolve_api_book_id_from_look(url)
        if api_book_id:
            return api_book_id, route["chapter_id"], _preferred_api_hosts(url)[0]
    return "", "", ""


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
    last_error = ""

    for attempt in range(1, retries + 1):
        try:
            if not local_input:
                book_id, chapter_id, api_host = _resolve_api_route(url)
                if book_id and chapter_id:
                    data, status_code, host = _fetch_api_chapter(book_id, chapter_id, preferred_url=api_host or url)
                    last_status = status_code
                    return _chapter_from_api_data(data, status_code, url, fallback_title=fallback_title)

            soup, status_code, final_url = _fetch_html_with_status(url, tries=1, referer=referer or BASE_URL)
            last_status = status_code if isinstance(status_code, int) else None
            if not local_input:
                book_id, chapter_id, api_host = _resolve_api_route(final_url)
                if book_id and chapter_id:
                    data, api_status, host = _fetch_api_chapter(book_id, chapter_id, preferred_url=api_host or final_url)
                    last_status = api_status
                    return _chapter_from_api_data(data, api_status, url, fallback_title=fallback_title)

            title = _chapter_title_from_page(soup, fallback=fallback_title, book_title=book_title)
            lines = _extract_chapter_paragraphs(soup, title=title)
            content_html, text = _content_html_from_lines(lines)
            if not _clean_spaces(text):
                raise FetchHtmlError("No chapter content", last_status)
            time.sleep(SLEEP_BETWEEN_CHAPS)
            return {
                "title": title,
                "content_html": content_html,
                "text": text,
                "url": url,
                "status_code": status_code,
                "parts": 1,
            }
        except FetchHtmlError as exc:
            last_status = exc.status_code
            last_error = str(exc)
            if attempt < retries:
                time.sleep(_retry_delay_seconds(last_status, attempt))
        except Exception as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(_retry_delay_seconds(last_status, attempt))

    return {
        "title": fallback_title or "Chapter error",
        "content_html": "<p>(Khong tai duoc noi dung)</p>",
        "text": "(Khong tai duoc noi dung)",
        "url": url,
        "status_code": last_status or "ERR",
        "error": last_error or "Download failed",
    }


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
    book_page_url = _ensure_url(book_page_url)
    cover_url = ""
    try:
        route = _route_from_url(book_page_url)
        if not _looks_like_local_file(book_page_url) and route.get("book_id"):
            info, host = _book_info_from_api(route["book_id"], preferred_url=book_page_url)
            cover_url = info.get("cover_url") or ""
        else:
            soup = _fetch_html(book_page_url)
            info = _get_book_info_from_soup(soup, book_page_url)
            cover_url = info.get("cover_url") or ""
    except Exception:
        cover_url = ""
    if not cover_url:
        return None, None, None
    try:
        if _looks_like_local_file(cover_url):
            path = Path(cover_url)
            content = path.read_bytes()
            ext = path.suffix.lower() or ".jpg"
            content, ext = _normalize_cover_image(content, ext)
            return content, ext, cover_url
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
    import download_policy
    import epub_builder

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
    parser = argparse.ArgumentParser(description="Download bqglll.cc novels")
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
