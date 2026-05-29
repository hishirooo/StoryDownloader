# -*- coding: utf-8 -*-
"""Downloader adapter for medoctruyen.vn."""

from __future__ import annotations

from bs4 import BeautifulSoup
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import argparse
import getpass
import html
import io
import json
import os
import re
import sys
import time
import unicodedata

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


BASE_URL = "https://medoctruyen.vn/"
DEFAULT_URL = "https://medoctruyen.vn/quy-di-tan-the-dong-vai-nguy-nhan-ta-tuc-tai-ach"
OUTPUT_BASE = Path("output")
COOKIE_PATH = Path(os.getenv("MEDOCTRUYEN_COOKIE_PATH") or "medoctruyen_cookie.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": BASE_URL,
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 30
SLEEP_BETWEEN_CHAPS = float(os.getenv("MEDOCTRUYEN_DELAY", "2.5"))
RATE_LIMIT_COOLDOWN = float(os.getenv("MEDOCTRUYEN_RATE_LIMIT_COOLDOWN", "25"))
CHAPTER_RETRIES = 3
RETRY_STATUS = {403, 408, 425, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)

_SESSION = http_requests.Session()
_COOKIES_LOADED = False
_LOGIN_ATTEMPTED = False


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


def _text(node) -> str:
    try:
        return node.get_text(" ", strip=True) if node else ""
    except Exception:
        return ""


def _clean_spaces(value: str) -> str:
    value = html.unescape(str(value or ""))
    value = value.replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()


def _ascii_fold(value: str) -> str:
    value = str(value or "").replace("đ", "d").replace("Đ", "D")
    value = unicodedata.normalize("NFKD", value)
    return value.encode("ascii", "ignore").decode("ascii", errors="ignore").lower()


def _safe_filename(name: str, max_length: int = 150) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', " - ", str(name or "book"))
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
    url = (url or "").strip().strip('"')
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
        return urljoin(BASE_URL, href)
    return urljoin(page_url if urlparse(page_url).scheme else BASE_URL, href)


def _normalized_url(url: str) -> str:
    if _looks_like_local_file(url):
        return str(Path(url))
    parsed = urlparse(url or "")
    return parsed._replace(query="", fragment="").geturl().rstrip("/")


def _session_cookies_dict() -> Dict[str, str]:
    cookies = getattr(_SESSION, "cookies", None)
    if cookies is None:
        return {}
    for name in ("get_dict",):
        fn = getattr(cookies, name, None)
        if callable(fn):
            try:
                return {str(k): str(v) for k, v in fn().items()}
            except Exception:
                pass
    try:
        return {str(k): str(v) for k, v in dict(cookies).items()}
    except Exception:
        return {}


def _has_session_cookies() -> bool:
    return bool(_session_cookies_dict())


def _load_cookies_once() -> bool:
    global _COOKIES_LOADED
    if _COOKIES_LOADED:
        return _has_session_cookies()
    _COOKIES_LOADED = True
    if not COOKIE_PATH.exists():
        return False
    try:
        data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            getattr(_SESSION, "cookies").update(data)
            return True
    except Exception as exc:
        _safe_print(f"[medoctruyen] Canh bao: khong doc duoc cookie cache: {exc}")
    return _has_session_cookies()


def _save_cookies() -> None:
    data = _session_cookies_dict()
    if not data:
        return
    try:
        COOKIE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        _safe_print(f"[medoctruyen] Canh bao: khong luu duoc cookie cache: {exc}")


def _response_json(response) -> Dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        try:
            return json.loads(getattr(response, "text", "") or "{}")
        except Exception:
            return {}


def _session_request(method: str, url: str, **kwargs: Any):
    headers = dict(HEADERS)
    referer = kwargs.pop("referer", None)
    if referer:
        headers["Referer"] = referer
    extra_headers = kwargs.pop("headers", None)
    if extra_headers:
        headers.update(extra_headers)
    request_kwargs: Dict[str, Any] = {
        "headers": headers,
        "timeout": kwargs.pop("timeout", TIMEOUT),
        "allow_redirects": kwargs.pop("allow_redirects", True),
    }
    request_kwargs.update(kwargs)
    if USE_CURL_CFFI:
        request_kwargs.setdefault("impersonate", "chrome110")
    request = getattr(_SESSION, method.lower())
    try:
        return request(url, **request_kwargs)
    except TypeError as exc:
        if USE_CURL_CFFI and "impersonate" in str(exc).lower():
            request_kwargs.pop("impersonate", None)
            return request(url, **request_kwargs)
        raise


def _can_prompt_login() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def _prompt_login_value(label: str, *, secret: bool = False) -> str:
    if not _can_prompt_login():
        return ""
    try:
        if secret:
            return getpass.getpass(label).strip()
        return input(label).strip()
    except (EOFError, KeyboardInterrupt):
        _safe_print("")
        return ""


def _verify_login_otp(login_token: str, otp: str) -> bool:
    verify = _session_request(
        "POST",
        urljoin(BASE_URL, "/api/auth/login/verify-otp"),
        referer=urljoin(BASE_URL, "/auth/login"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        json={"login_token": login_token, "code": otp},
    )
    verify_status = getattr(verify, "status_code", 0) or 0
    if 200 <= verify_status < 300:
        _save_cookies()
        _safe_print("[medoctruyen] Da xac thuc OTP va luu cookie cache.")
        return True
    verify_data = _response_json(verify)
    message = verify_data.get("error", {}).get("message") if isinstance(verify_data.get("error"), dict) else ""
    _safe_print(f"[medoctruyen] OTP khong hop le: HTTP {verify_status} {message}".strip())
    return False


def login(
    email: Optional[str] = None,
    password: Optional[str] = None,
    otp: Optional[str] = None,
    *,
    prompt_missing: bool = False,
) -> bool:
    """Login with args, MEDOCTRUYEN_* env vars, or terminal prompts."""
    global _LOGIN_ATTEMPTED
    _LOGIN_ATTEMPTED = True
    email = (email or os.getenv("MEDOCTRUYEN_EMAIL") or "").strip()
    password = password or os.getenv("MEDOCTRUYEN_PASSWORD") or ""
    otp = (otp or os.getenv("MEDOCTRUYEN_OTP") or "").strip()
    if prompt_missing and not email:
        email = _prompt_login_value("Email medoctruyen (Enter de bo qua): ")
    if prompt_missing and email and not password:
        password = _prompt_login_value("Mat khau medoctruyen: ", secret=True)
    if not email or not password:
        if prompt_missing:
            _safe_print("[medoctruyen] Bo qua dang nhap vi thieu email/mat khau.")
        return False

    url = urljoin(BASE_URL, "/api/auth/login")
    try:
        response = _session_request(
            "POST",
            url,
            referer=urljoin(BASE_URL, "/auth/login"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"email": email, "password": password},
        )
        status_code = getattr(response, "status_code", 0) or 0
        data = _response_json(response)
        if status_code < 200 or status_code >= 300:
            message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else ""
            _safe_print(f"[medoctruyen] Dang nhap that bai: HTTP {status_code} {message}".strip())
            return False

        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        if data.get("skip_otp") or nested.get("skip_otp"):
            _save_cookies()
            _safe_print("[medoctruyen] Da dang nhap va luu cookie cache.")
            return True

        login_token = data.get("login_token") or nested.get("login_token") or ""
        if login_token and otp:
            return _verify_login_otp(login_token, otp)

        if login_token:
            if prompt_missing:
                otp = _prompt_login_value("Nhap ma OTP 6 so (Enter de bo qua): ")
                if otp:
                    return _verify_login_otp(login_token, otp)
            _safe_print("[medoctruyen] Tai khoan can OTP. Chay lai voi --login --otp <ma_otp> hoac nhap OTP khi duoc hoi.")
        else:
            _safe_print("[medoctruyen] Dang nhap khong tra ve token hop le.")
    except Exception as exc:
        _safe_print(f"[medoctruyen] Dang nhap loi: {exc}")
    return False


def _maybe_login(force: bool = False) -> bool:
    global _LOGIN_ATTEMPTED
    had_cookie = _load_cookies_once()
    if had_cookie and not force:
        return True
    if _LOGIN_ATTEMPTED:
        return _has_session_cookies()
    if os.getenv("MEDOCTRUYEN_EMAIL") and os.getenv("MEDOCTRUYEN_PASSWORD"):
        return login()
    if force:
        _safe_print("[medoctruyen] Can dang nhap de lay noi dung day du.")
        return login(prompt_missing=True)
    if not os.getenv("MEDOCTRUYEN_EMAIL") or not os.getenv("MEDOCTRUYEN_PASSWORD"):
        return had_cookie
    return False


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
    candidates.extend(["utf-8", "utf-8-sig"])
    for enc in candidates:
        if not enc:
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

    _maybe_login()
    last_error: Optional[Exception] = None
    last_status: Any = None
    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(backoff * attempt)
        try:
            response = _session_request("GET", url, referer=referer or BASE_URL)
            status_code = getattr(response, "status_code", 200)
            last_status = status_code
            if status_code in RETRY_STATUS and attempt < tries:
                continue
            if status_code != 200:
                raise FetchHtmlError(f"HTTP {status_code}: {url}", status_code)
            content = getattr(response, "content", b"")
            if not content:
                text = getattr(response, "text", "")
                content = text.encode(getattr(response, "encoding", "utf-8") or "utf-8", errors="replace")
            return BeautifulSoup(_decode_html(content, response), "html.parser"), status_code, getattr(response, "url", url)
        except FetchHtmlError as exc:
            last_error = exc
            last_status = exc.status_code
            if attempt >= tries:
                break
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


def _meta_content(soup: BeautifulSoup, selector: str) -> str:
    node = soup.select_one(selector)
    return _clean_spaces(node.get("content", "")) if node else ""


def _json_ld_items(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            graph = value.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    add(item)
            items.append(value)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=False)
        if not raw:
            continue
        try:
            add(json.loads(raw))
        except Exception:
            continue
    return items


def _ld_type_matches(item: Dict[str, Any], target: str) -> bool:
    value = item.get("@type")
    if isinstance(value, list):
        return any(str(part).lower() == target.lower() for part in value)
    return str(value or "").lower() == target.lower()


def _first_ld(soup: BeautifulSoup, target_type: str) -> Dict[str, Any]:
    for item in _json_ld_items(soup):
        if _ld_type_matches(item, target_type):
            return item
    return {}


def _author_name(value: Any) -> str:
    if isinstance(value, dict):
        return _clean_spaces(str(value.get("name") or ""))
    if isinstance(value, list):
        names = [_author_name(item) for item in value]
        return ", ".join(name for name in names if name)
    return _clean_spaces(str(value or ""))


def _genre_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_clean_spaces(str(item)) for item in value if _clean_spaces(str(item)))
    return _clean_spaces(str(value or ""))


def _strip_site_suffix(title: str) -> str:
    title = _clean_spaces(title)
    title = re.sub(r"\s*\|\s*Mê Đọc Truyện\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*-\s*Truyện\s+[^|]+$", "", title, flags=re.I)
    return _clean_spaces(title)


def _category_from_title(title: str) -> str:
    match = re.search(r"-\s*Truyện\s+([^|]+)$", title or "", flags=re.I)
    return _clean_spaces(match.group(1)) if match else ""


def _story_url_from_soup(soup: BeautifulSoup, source_url: str) -> str:
    book = _first_ld(soup, "Book")
    article = _first_ld(soup, "Article")
    part_of = article.get("isPartOf") if isinstance(article.get("isPartOf"), dict) else {}
    candidates = [
        str(book.get("url") or ""),
        str(part_of.get("url") or ""),
        _meta_content(soup, 'meta[property="og:url"]'),
        (soup.select_one('link[rel="canonical"]') or {}).get("href", "") if soup.select_one('link[rel="canonical"]') else "",
        source_url,
    ]
    for candidate in candidates:
        candidate = html.unescape(str(candidate or "")).strip()
        if not re.match(r"^https?://", candidate, flags=re.I):
            continue
        parsed = urlparse(candidate)
        path = re.sub(r"/chuong-\d+/?$", "", parsed.path.rstrip("/"), flags=re.I) or "/"
        return parsed._replace(path=path, query="", fragment="").geturl().rstrip("/")
    return DEFAULT_URL


def _chapter_number_from_url(url: str) -> Optional[int]:
    match = re.search(r"/chuong-(\d+)(?:/)?(?:[?#].*)?$", url or "", flags=re.I)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _story_url_from_chapter_url(url: str) -> str:
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return url
    path = re.sub(r"/chuong-\d+/?$", "", parsed.path.rstrip("/"), flags=re.I)
    if path == parsed.path.rstrip("/"):
        return url
    return parsed._replace(path=path or "/", query="", fragment="").geturl().rstrip("/")


def _make_chapter_url(story_url: str, number: int) -> str:
    return story_url.rstrip("/") + f"/chuong-{number}"


def _clean_chapter_title(title: str) -> str:
    title = _clean_spaces(title)
    title = re.sub(r"\s+(?:Miễn phí|VIP|Đã mua|Khóa|Khoá)\s*$", "", title, flags=re.I)
    return _clean_spaces(title)


def _rendered_chapter_titles(soup: BeautifulSoup, page_url: str) -> Dict[int, str]:
    result: Dict[int, str] = {}
    for a in soup.select('a[href*="/chuong-"]'):
        href = a.get("href", "")
        url = _absolute_url(page_url, href)
        number = _chapter_number_from_url(url)
        if not number:
            continue
        title = _clean_chapter_title(a.get("title") or _text(a))
        if not title or title in {"Chương trước", "Chương sau", "Đọc từ đầu"}:
            continue
        if not re.search(r"\bChương\s+\d+\b", title, flags=re.I):
            continue
        result.setdefault(number, title)
    return result


def _extract_total_chapters(soup: BeautifulSoup, book: Dict[str, Any], title_map: Dict[int, str]) -> int:
    candidates: List[int] = []
    for value in (book.get("numberOfPages"), book.get("numberOfChapters")):
        try:
            number = int(str(value or "").replace(",", "").strip())
            if number > 0:
                candidates.append(number)
        except ValueError:
            pass
    for node in soup.select("input[max]"):
        try:
            number = int(str(node.get("max") or "").strip())
            if number > 0:
                candidates.append(number)
        except ValueError:
            pass
    text = soup.get_text(" ", strip=True)
    for match in re.finditer(r"([0-9][0-9\.,]*)\s+chương", text, flags=re.I):
        raw = match.group(1).replace(".", "").replace(",", "")
        try:
            number = int(raw)
            if number > 0:
                candidates.append(number)
        except ValueError:
            pass
    if title_map:
        candidates.append(max(title_map))
    return max(candidates) if candidates else 0


def _catalog_page_count(soup: BeautifulSoup, total_chapters: int = 0) -> int:
    pages = []
    for a in soup.select('a[href*="?ch="]'):
        href = html.unescape(a.get("href", ""))
        match = re.search(r"[?&]ch=(\d+)", href)
        if match:
            try:
                pages.append(int(match.group(1)))
            except ValueError:
                pass
    if pages:
        return max(pages)
    if total_chapters:
        return max(1, (total_chapters + 23) // 24)
    return 1


def _should_fetch_catalog_pages() -> bool:
    raw = (os.getenv("MEDOCTRUYEN_FETCH_CATALOG_PAGES") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _complete_catalog_titles(first_soup: BeautifulSoup, story_url: str, *, local_input: bool, total_chapters: int) -> Dict[int, str]:
    titles = _rendered_chapter_titles(first_soup, story_url)
    if local_input or not _should_fetch_catalog_pages():
        return titles
    total_pages = _catalog_page_count(first_soup, total_chapters)
    if total_pages <= 1:
        return titles

    for page in range(2, total_pages + 1):
        page_url = f"{story_url}?ch={page}#danh-sach-chuong"
        try:
            if page == 2 or page == total_pages or page % 10 == 0:
                _safe_print(f"[medoctruyen] Doc muc luc trang {page}/{total_pages}...")
            soup, _status, _final_url = _fetch_html_with_status(page_url, tries=2, referer=story_url)
            titles.update(_rendered_chapter_titles(soup, story_url))
            time.sleep(0.12)
        except Exception as exc:
            _safe_print(f"[medoctruyen] Canh bao: bo qua muc luc trang {page}: {exc}")
    return titles


def _latest_from_titles(titles: Dict[int, str], story_url: str) -> Tuple[str, str]:
    if not titles:
        return "", ""
    number = max(titles)
    return titles[number], _make_chapter_url(story_url, number)


def _get_book_info_from_soup(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, Any]:
    book = _first_ld(soup, "Book")
    article = _first_ld(soup, "Article")
    part_of = article.get("isPartOf") if isinstance(article.get("isPartOf"), dict) else {}
    story_url = _story_url_from_soup(soup, page_url)
    og_title = _meta_content(soup, 'meta[property="og:title"]')
    title = _clean_spaces(str(book.get("name") or part_of.get("name") or "")) or _strip_site_suffix(_text(soup.select_one("h1")) or og_title)
    category = _genre_text(book.get("genre")) or _category_from_title(og_title)
    intro = _clean_spaces(str(book.get("description") or article.get("description") or "")) or _meta_content(soup, 'meta[property="og:description"]')
    cover_url = _clean_spaces(str(book.get("image") or article.get("image") or "")) or _meta_content(soup, 'meta[property="og:image"]')
    author = _author_name(book.get("author"))
    if not author:
        author = _author_name(article.get("author"))
    if not author:
        description = _meta_content(soup, 'meta[name="description"]') or _meta_content(soup, 'meta[property="og:description"]')
        match = re.search(r"của tác giả\s+([^,\.]+)", description, flags=re.I)
        if match:
            author = _clean_spaces(match.group(1))
    status = ""
    body_text = soup.get_text(" ", strip=True)
    for marker in ("Hoàn thành", "Đang ra", "Tạm ngưng"):
        if marker.lower() in body_text.lower():
            status = marker
            break

    rendered_titles = _rendered_chapter_titles(soup, story_url)
    total_chapters = _extract_total_chapters(soup, book, rendered_titles)
    latest_chapter, latest_chapter_url = _latest_from_titles(rendered_titles, story_url)

    return {
        "title": title or "Unknown",
        "author": author or "Unknown",
        "status": status,
        "category": category,
        "genre": category,
        "update_time": "",
        "latest_chapter": latest_chapter,
        "latest_chapter_url": latest_chapter_url,
        "cover_url": _absolute_url(page_url, cover_url) if cover_url else "",
        "intro": intro,
        "url": story_url,
        "total_chapters": total_chapters,
    }


def getText(url: str = DEFAULT_URL) -> Dict[str, Any]:
    url = _ensure_url(url)
    local_input = _looks_like_local_file(url)
    if not local_input:
        url = _story_url_from_chapter_url(url)
    soup = _fetch_html(url)
    info = _get_book_info_from_soup(soup, url)
    story_url = info.get("url") or _story_url_from_soup(soup, url)
    total = int(info.get("total_chapters") or 0)
    first_titles = _rendered_chapter_titles(soup, story_url)
    if total <= 0:
        total = _extract_total_chapters(soup, _first_ld(soup, "Book"), first_titles)

    titles = _complete_catalog_titles(soup, story_url, local_input=local_input, total_chapters=total)
    if total <= 0 and titles:
        total = max(titles)
    if total <= 0:
        total = 1 if _chapter_number_from_url(story_url) else 0

    chapters = [
        {"title": titles.get(number) or f"Chương {number}", "url": _make_chapter_url(story_url, number)}
        for number in range(1, total + 1)
    ]
    if titles:
        latest_chapter, latest_chapter_url = _latest_from_titles(titles, story_url)
        info["latest_chapter"] = latest_chapter
        info["latest_chapter_url"] = latest_chapter_url
    if chapters:
        info["total_chapters"] = len(chapters)
        if not info.get("latest_chapter"):
            info["latest_chapter"] = chapters[-1]["title"]
            info["latest_chapter_url"] = chapters[-1]["url"]
    return {**info, "chapters": chapters}


def _chapter_title_from_page(soup: BeautifulSoup, fallback: str = "", book_title: str = "") -> str:
    article = _first_ld(soup, "Article")
    title = _clean_spaces(str(article.get("headline") or ""))
    if not title:
        title = _text(soup.select_one("h1"))
    if not title:
        title = _meta_content(soup, 'meta[property="og:title"]') or _text(soup.find("title"))
    title = _strip_site_suffix(title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title) :].strip(" -_:：")
    match = re.search(r"(Chương\s+\d+\s*:?\s*.*)$", title, flags=re.I)
    if match:
        title = match.group(1)
    return _clean_chapter_title(title) or fallback or "Chapter"


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ").replace("\u3000", " ")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    trash = re.compile(
        r"(Mê Đọc Truyện|Trang chủ|Giới thiệu|D\.S Chương|Bình luận|Chương trước|Chương sau|"
        r"Đọc từ đầu|Thêm vào kệ|Đăng nhập để bình luận|Copyright)",
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
        if re.fullmatch(r"https?://\S+", line, flags=re.I):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return lines


def _content_html_from_lines(lines: Iterable[str]) -> Tuple[str, str]:
    lines = [str(line).strip() for line in lines if str(line).strip()]
    text = "\n".join(lines).strip()
    content_html = "\n".join(f"<p>{html.escape(line)}</p>" for line in lines)
    return content_html, text


def _extract_chapter_lines(soup: BeautifulSoup, title: str = "") -> List[str]:
    content = soup.select_one("article") or soup.select_one("main article")
    if not content:
        candidates = [node for node in soup.select("main, body") if len(_clean_spaces(node.get_text(" ", strip=True))) > 300]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return []
    content = BeautifulSoup(str(content), "html.parser")
    for node in content.find_all(["script", "style", "noscript", "iframe", "form", "button", "input", "select"]):
        node.decompose()
    for selector in ("nav", "header", "footer", ".ads", ".ad", "[data-preview-gate]", "[data-reader-toolbar]"):
        for node in content.select(selector):
            node.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")
    return _clean_chapter_lines(content.get_text("\n", strip=False), title=title)


def _looks_like_locked_or_login_page(soup: BeautifulSoup, text: str) -> bool:
    if soup.select_one("[data-preview-gate]"):
        return True
    full = _clean_spaces(text or soup.get_text(" ", strip=True)).lower()
    ascii_full = _ascii_fold(full)
    ascii_markers = (
        "dang nhap de doc tiep",
        "dang nhap de doc",
        "vui long dang nhap",
        "mua chuong",
        "mo khoa chuong",
        "khong du lua",
        "noi dung day du",
    )
    if any(marker in ascii_full for marker in ascii_markers):
        return True
    markers = (
        "đăng nhập để đọc tiếp",
        "đăng nhập để đọc",
        "vui lòng đăng nhập",
        "mua chương",
        "mở khóa chương",
        "mo khoa chuong",
        "không đủ lúa",
        "khong du lua",
        "nội dung đầy đủ",
        "noi dung day du",
    )
    return any(marker in full for marker in markers)


def _looks_like_rate_limited(soup: BeautifulSoup, text: str) -> bool:
    folded = _ascii_fold(text or soup.get_text(" ", strip=True))
    markers = (
        "ban doc qua nhanh",
        "doc qua nhanh",
        "vui long cho trong giay lat",
        "cho trong giay lat de co the doc chuong tiep theo",
    )
    return any(marker in folded for marker in markers)


def _retry_delay_seconds(status_code: Any, attempt: int) -> float:
    if status_code == 403:
        return min(12.0, 3.0 + attempt * 2.0)
    if status_code == 429:
        return min(90.0, RATE_LIMIT_COOLDOWN + 10.0 * (attempt - 1))
    return max(SLEEP_BETWEEN_CHAPS, 1.0 * attempt)


def fetch_chapter_content(
    url: str,
    retries: int = CHAPTER_RETRIES,
    *,
    fallback_title: str = "",
    book_title: str = "",
    referer: str = "",
) -> Dict[str, Any]:
    url = _ensure_url(url)
    local_input = _looks_like_local_file(url)
    last_status: Any = None
    last_error = ""
    last_title = fallback_title or ""
    total_attempts = 1 if local_input else max(int(retries or 1), 2)

    for attempt in range(1, total_attempts + 1):
        try:
            soup, status_code, _final_url = _fetch_html_with_status(url, tries=1, referer=referer or BASE_URL)
            last_status = status_code if isinstance(status_code, int) else None
            title = _chapter_title_from_page(soup, fallback=fallback_title, book_title=book_title)
            last_title = title or last_title
            lines = _extract_chapter_lines(soup, title=title)
            content_html, text = _content_html_from_lines(lines)

            if not local_input and (not text or _looks_like_locked_or_login_page(soup, text)):
                if _maybe_login(force=True):
                    soup, status_code, _final_url = _fetch_html_with_status(url, tries=1, referer=referer or BASE_URL)
                    last_status = status_code if isinstance(status_code, int) else None
                    title = _chapter_title_from_page(soup, fallback=fallback_title, book_title=book_title)
                    last_title = title or last_title
                    lines = _extract_chapter_lines(soup, title=title)
                    content_html, text = _content_html_from_lines(lines)

            if not text:
                raise FetchHtmlError("No chapter content", last_status)
            if _looks_like_rate_limited(soup, text):
                raise FetchHtmlError("Rate limit: reading too fast", 429)
            if _looks_like_locked_or_login_page(soup, text):
                raise FetchHtmlError("Login/purchase gate detected", last_status or 200)
            if not local_input and SLEEP_BETWEEN_CHAPS > 0:
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
            last_error = str(exc)
            if "Login/purchase gate" in last_error:
                last_status = "ERR_LOGIN"
            elif "Rate limit" in last_error:
                last_status = 429
            elif "No chapter content" in last_error and exc.status_code in (None, 200):
                last_status = "ERR_CONTENT"
            else:
                last_status = exc.status_code
            if attempt < total_attempts:
                time.sleep(_retry_delay_seconds(last_status, attempt))
        except Exception as exc:
            last_error = str(exc)
            if attempt < total_attempts:
                time.sleep(_retry_delay_seconds(last_status, attempt))

    return {
        "title": last_title or "Chapter error",
        "content_html": "",
        "text": "",
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
        response = _session_request("GET", cover_url, referer=book_page_url, stream=True)
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
    book_info: Dict[str, Any],
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
    global SLEEP_BETWEEN_CHAPS, RATE_LIMIT_COOLDOWN
    parser = argparse.ArgumentParser(description="Download medoctruyen.vn novels")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--no-epub", action="store_true")
    parser.add_argument("--delay", type=float, default=None, help="Seconds to wait after each chapter request")
    parser.add_argument("--cooldown", type=float, default=None, help="Seconds to wait after a reading-too-fast block")
    parser.add_argument("--login", action="store_true", help="Login before downloading; prompts when values are missing")
    parser.add_argument("--email", default="", help="Login email for medoctruyen.vn")
    parser.add_argument("--password", default="", help="Login password for medoctruyen.vn")
    parser.add_argument("--otp", default="", help="OTP code if the site asks for it")
    parser.add_argument("-y", "--yes", action="store_true", help="Run non-interactively")
    args = parser.parse_args(argv)

    if args.delay is not None:
        SLEEP_BETWEEN_CHAPS = max(0.0, args.delay)
    if args.cooldown is not None:
        RATE_LIMIT_COOLDOWN = max(1.0, args.cooldown)

    if args.login or args.email or args.password or args.otp:
        login(args.email, args.password, args.otp, prompt_missing=args.login)
    data = getText(args.url)
    title = data.get("title") or "Book"
    chapters = data.get("chapters") or []
    out_dir = OUTPUT_BASE / slugify_vi(title)
    out_dir.mkdir(parents=True, exist_ok=True)
    _safe_print(f"Title: {title}")
    _safe_print(f"Author: {data.get('author', 'Unknown')}")
    if data.get("category") or data.get("genre"):
        _safe_print(f"Category: {data.get('category') or data.get('genre')}")
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

'''
hướng dẫn sử dụng lệnh để tải truyện:
python medoctruyen.py <url> [--start N] [--end M]
Trong đó:
- <url>: URL của truyện trên medoctruyen.vn (có thể là
    trang truyện hoặc trang chương)
- --start N: Bắt đầu tải từ chương N (mặc định là 1)
- --end M: Kết thúc tải tại chương M (mặc định là chương cuối cùng)
- --no-epub: Chỉ lưu chương dưới dạng HTML, không tạo file EPUB
- --delay SECONDS: Thời gian chờ sau mỗi lần tải chương (mặc định là 0.12 giây)
- --cooldown SECONDS: Thời gian chờ sau khi bị chặn do đọc quá nhanh (mặc định là 30 giây)
- --login: Đăng nhập trước khi tải; sẽ hỏi thông tin nếu chưa có
- --email EMAIL: Email đăng nhập cho medoctruyen.vn
- --password PASSWORD: Mật khẩu đăng nhập cho medoctruyen.vn
- --otp OTP: Mã OTP nếu trang yêu cầu
- -y, --yes: Chạy không tương tác, trả lời "yes" cho tất cả các câu hỏi xác nhận
Ví dụ:
python medoctruyen.py https://medoctruyen.vn/truyen/duong-son-ky --start 10 --end 50
Lệnh trên sẽ tải các chương từ 10 đến 50 của truyện "Dương Sơn Kỳ" và lưu chúng vào một file EPUB trong thư mục output.

python medoctruyen.py https://medoctruyen.vn/truyen/duong-son-ky --no-epub
Lệnh trên sẽ tải tất cả các chương của truyện "Dương Sơn Kỳ" và lưu chúng dưới dạng các file HTML riêng biệt trong thư mục output/duong-son-ky

python medoctruyen.py https://medoctruyen.vn/truyen/duong-son-ky --login
Lệnh trên sẽ yêu cầu bạn đăng nhập vào tài khoản medoctruyen.vn trước khi tải truyện, điều này có thể cần thiết nếu truyện yêu cầu đăng nhập để xem nộidung hoặc nếu bạn muốn tránh bị chặn do đọc quá nhanh. Bạn sẽ được hỏi nhập email, mật khẩu và có thể là mã OTP nếu tài khoản của bạn có bảo mật 2 lớp. Sau khi đăng nhập thành công, chương trình sẽ tiếp tục tải truyện như bình thường

python medoctruyen.py https://medoctruyen.vn/truyen/duong-son-ky --delay 1.0 --cooldown 60
Lệnh trên sẽ thiết lập thời gian chờ là 1 giây sau mỗi lần tải chương và 60 giây nếu bị chặn do đọc quá nhanh. Điều này có thể giúp tránh bị chặn khi tải các truyện có nhiều chương hoặc khi tải với tốc độ cao.

python medoctruyen.py https://medoctruyen.vn/truyen/duong-son-ky --start 10 --end 20 --no-epub
Lệnh trên sẽ tải các chương từ 10 đến 20 của truyện "Dương Sơn Kỳ" và lưu chúng dưới dạng các file HTML riêng biệt trong thư mục output/duong-son-ky, mà không tạo file EPUB. Đây là cách tốt nếu bạn chỉ muốn có nội dung của một số chương nhất định mà không cần toàn bộ truyện hoặc nếu bạn muốn xử lý nội dung theo cách riêng của mình sau khi tải về.

python medoctruyen.py https://medoctruyen.vn/truyen/duong-son-ky --yes
Lệnh trên sẽ chạy chương trình ở chế độ không tương tác, tự động trả lời "yes" cho tất cả các câu hỏi xác nhận. Điều này có thể hữu ích khi bạn muốntải truyện mà không cần phải tương tác với chương trình, ví dụ khi chạy trong một môi trường tự động hoặc khi bạn đã chắc chắn về các lựa chọn của mình và không muốn bị gián đoạn bởi các câu hỏi xác nhận. Tuy nhiên, hãy cẩn thận khi sử dụng tùy chọn này, vì nó sẽ tự động đồng ý với tất cả các xác nhận, bao gồm cả những xác nhận có thể dẫn đến việc ghi đè file hoặc tải về một số lượng lớn dữ liệu mà bạn không mong muốn.

python medoctruyen.py https://medoctruyen.vn/truyen/duong-son-ky --login --delay 2.0 --cooldown 120
Lệnh trên sẽ yêu cầu bạn đăng nhập vào tài khoản medoctruyen.vn trước khi tải truyện, và thiết lập thời gian chờ là 2 giây sau mỗi lần tải chương và 120 giây nếu bị chặn do đọc quá nhanh. Đây là cách tốt để đảm bảo rằng bạn có thể tải được nội dung của truyện mà không bị chặn, đặc biệt là đối với những truyện có nhiều chương hoặc khi bạn muốn tải với tốc độ cao nhưng vẫn muốn tránh bị chặn. Việc đăng nhập cũng có thể giúp bạn truy cập vào nội dung của những truyện yêu cầu đăng nhập để xem hoặc giúp tránh bị chặn do đọc quá nhanh, vì một số trang có thể cho phép người dùng đã đăng nhập tải với tốc độ cao hơn mà không bị chặn.
'''
