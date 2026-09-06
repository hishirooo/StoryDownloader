# -*- coding: utf-8 -*-
"""Downloader adapter for dienha.com."""

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

BASE_URL = "https://dienha.com/"
DEFAULT_URL = "https://dienha.com/truyen/khoa-thuy-tinh"
OUTPUT_BASE = Path("output")
COOKIE_PATH = Path(os.getenv("DIENHA_COOKIE_PATH") or "dienha_cookie.json")

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
SLEEP_BETWEEN_CHAPS = float(os.getenv("DIENHA_DELAY", "2.5"))
RATE_LIMIT_COOLDOWN = float(os.getenv("DIENHA_RATE_LIMIT_COOLDOWN", "25"))
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

def _clean_spaces(value: str) -> str:
    value = html.unescape(str(value or ""))
    value = value.replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()

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
        _safe_print(f"[dienha] Canh bao: khong doc duoc cookie cache: {exc}")
    return _has_session_cookies()

def _save_cookies() -> None:
    data = _session_cookies_dict()
    if not data:
        return
    try:
        COOKIE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        _safe_print(f"[dienha] Canh bao: khong luu duoc cookie cache: {exc}")

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

def _fetch_html(url: str, **kwargs: Any) -> BeautifulSoup:
    _load_cookies_once()
    if _looks_like_local_file(url):
        try:
            content = Path(url).read_bytes()
            return BeautifulSoup(content, "html.parser")
        except Exception as exc:
            raise FetchHtmlError(f"Local file error: {exc}")

    try:
        response = _session_request("GET", url, **kwargs)
        status = getattr(response, "status_code", 200)
        
        # Save cookies if successful
        if status == 200 and _has_session_cookies():
            _save_cookies()

        if status == 429:
            raise FetchHtmlError(f"Rate limit exceeded (429) cho {url}", status_code=429)
        elif status != 200:
            raise FetchHtmlError(f"HTTP {status} cho {url}", status_code=status)
            
        content = getattr(response, "content", b"")
        return BeautifulSoup(content, "html.parser")
    except FetchHtmlError:
        raise
    except Exception as exc:
        raise FetchHtmlError(f"Request error: {exc}")

def login(email: str = "", password: str = "", otp: str = "", *, prompt_missing: bool = True) -> bool:
    _safe_print("[dienha] Tinh nang login tu dong hien chua ho tro, vui long su dung file cookie neu can.")
    return False

def getText(url: str) -> Dict[str, Any]:
    url = _ensure_url(url)
    
    if "/doc/" in url and not _looks_like_local_file(url):
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 2 and path_parts[0] == "doc":
            slug = path_parts[1]
            url = parsed._replace(path=f"/truyen/{slug}").geturl()

    try:
        soup = _fetch_html(url)
    except FetchHtmlError as exc:
        _safe_print(f"Loi tai trang thong tin: {exc}")
        return {"url": url, "chapters": []}
    
    title_node = soup.select_one('h1.ct-ten')
    title = title_node.get_text(strip=True) if title_node else "Book"

    author_node = soup.select_one('.ct-nhan .nhan')
    author = author_node.get_text(strip=True) if author_node else "Unknown"
    author = re.sub(r'^\s*[\W_]+\s*', '', author) 
    
    cover_node = soup.select_one('#o-bia img')
    cover_url = cover_node.get('src') if cover_node else ""
    if cover_url:
        cover_url = _absolute_url(url, cover_url)

    categories = []
    for a in soup.select('.ct-chips a'):
        categories.append(a.get_text(strip=True))
    category = ", ".join(categories)

    chapters = []
    for a in soup.select('a[data-chuong-so]'):
        c_url = a.get('href')
        c_num = a.get('data-chuong-so')
        if c_url:
            c_url = _absolute_url(url, c_url)
            t_node = a.select_one('.t')
            c_title = t_node.get_text(strip=True) if t_node else f"Chương {c_num}"
            chapters.append({"url": c_url, "title": c_title})

    return {
        "title": title,
        "author": author,
        "url": url,
        "cover_url": cover_url,
        "category": category,
        "chapters": chapters,
        "source": "dienha",
    }

def _retry_delay_seconds(status: Any, attempt: int) -> float:
    if str(status) == "429":
        return max(5.0, RATE_LIMIT_COOLDOWN * attempt)
    return max(1.0, SLEEP_BETWEEN_CHAPS * (2 ** (attempt - 1)))

def fetch_chapter_content(
    url: str,
    retries: int = CHAPTER_RETRIES,
    *,
    fallback_title: str = "",
    book_title: str = "",
    referer: str = "",
) -> Dict[str, Any]:
    last_error = ""
    last_status = None
    last_title = fallback_title or "Chapter"
    
    attempt = 1
    total_attempts = max(1, retries + 1)

    while attempt <= total_attempts:
        try:
            soup = _fetch_html(url)
            title_node = soup.select_one('div.dc-tieu-de h1')
            if title_node:
                last_title = title_node.get_text(strip=True)
            
            content_div = soup.select_one('div#noi')
            if not content_div:
                raise FetchHtmlError("No chapter content found (div#noi)", status_code=200)

            for btn in content_div.select('button.dc-nut-doan'):
                btn.decompose()
            for span in content_div.select('span.dc-cuoi-doan'):
                span.unwrap() 

            html_content = ""
            text_content = ""
            for p in content_div.select('p'):
                html_content += str(p)
                text_content += p.get_text(" ", strip=True) + "\n"

            if not text_content.strip():
                raise FetchHtmlError("Chapter content is empty", status_code=200)

            return {
                "title": last_title,
                "content_html": f'<div class="chapter-content">{html_content}</div>',
                "text": text_content.strip(),
                "url": url,
                "status_code": 200,
                "error": "",
                "parts": 1,
            }

        except FetchHtmlError as exc:
            last_error = str(exc)
            last_status = exc.status_code
            if attempt < total_attempts:
                time.sleep(_retry_delay_seconds(last_status, attempt))
        except Exception as exc:
            last_error = str(exc)
            if attempt < total_attempts:
                time.sleep(_retry_delay_seconds(last_status, attempt))
        attempt += 1

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
    try:
        info = getText(book_page_url)
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
    parser = argparse.ArgumentParser(description="Download dienha.com novels")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--no-epub", action="store_true")
    parser.add_argument("--delay", type=float, default=None, help="Seconds to wait after each chapter request")
    parser.add_argument("--cooldown", type=float, default=None, help="Seconds to wait after a reading-too-fast block")
    parser.add_argument("--login", action="store_true", help="Login before downloading; prompts when values are missing")
    parser.add_argument("--email", default="", help="Login email for dienha.com")
    parser.add_argument("--password", default="", help="Login password for dienha.com")
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
python dienha.py <url> [--start N] [--end M]
Trong đó:
- <url>: URL của truyện trên dienha.com (có thể là trang truyện hoặc trang chương)
- --start N: Bắt đầu tải từ chương N (mặc định là 1)
- --end M: Kết thúc tải tại chương M (mặc định là chương cuối cùng)
- --no-epub: Chỉ lưu chương dưới dạng HTML, không tạo file EPUB
- --delay SECONDS: Thời gian chờ sau mỗi lần tải chương
- --cooldown SECONDS: Thời gian chờ sau khi bị chặn do đọc quá nhanh
Ví dụ:
python dienha.py https://dienha.com/truyen/khoa-thuy-tinh --start 10 --end 50
'''
