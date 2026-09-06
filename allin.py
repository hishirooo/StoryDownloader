# -*- coding: utf-8 -*-
"""Downloader adapter for allin.vn."""

from __future__ import annotations

from bs4 import BeautifulSoup
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import argparse
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

# ============================================================
#  Cau hinh
# ============================================================
BASE_URL        = "https://allin.vn"
API_BASE_URL    = "https://api.allin.vn"
DEFAULT_URL     = "https://allin.vn/truyen/minh-nguyet-do-kiem"
OUTPUT_BASE     = Path("output")
COOKIE_PATH     = Path(os.getenv("ALLIN_COOKIE_PATH") or "allin_cookie.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Referer": BASE_URL,
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT                = 30
SLEEP_BETWEEN_CHAPS    = float(os.getenv("ALLIN_DELAY", "2.0"))
RATE_LIMIT_COOLDOWN    = float(os.getenv("ALLIN_RATE_LIMIT_COOLDOWN", "20.0"))
CHAPTER_RETRIES        = 3
RETRY_STATUS           = {403, 408, 425, 429, 500, 502, 503, 504}
MAX_COVER_SIZE         = (1600, 2400)

_SESSION         = http_requests.Session()
_COOKIES_LOADED  = False
_LOGIN_ATTEMPTED = False


# ============================================================
#  Utility
# ============================================================
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
    value = str(value or "").replace("\u0111", "d").replace("\u0110", "D")
    value = unicodedata.normalize("NFKD", value)
    return value.encode("ascii", "ignore").decode("ascii", errors="ignore").lower()


def _safe_filename(name: str, max_length: int = 150) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', " - ", str(name or "book"))
    safe = re.sub(r"\s+", " ", safe).strip().strip(".")
    return (safe[:max_length].strip() or "book")


def _slugify(value: str) -> str:
    value = _ascii_fold(value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "truyen"


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


# ============================================================
#  Session / Cookie / HTTP
# ============================================================
def _session_cookies_dict() -> Dict[str, str]:
    cookies = getattr(_SESSION, "cookies", None)
    if cookies is None:
        return {}
    fn = getattr(cookies, "get_dict", None)
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
        return _has_session_cookies() or "x-access-token" in HEADERS
    _COOKIES_LOADED = True
    if not COOKIE_PATH.exists():
        return False
    try:
        data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            if "x-access-token" in data:
                HEADERS["x-access-token"] = data["x-access-token"]
                _safe_print(f"[allin] Da tai token tu: {COOKIE_PATH}")
                return True
            else:
                getattr(_SESSION, "cookies").update(data)
                _safe_print(f"[allin] Da tai cookie tu: {COOKIE_PATH}")
                return True
    except Exception as exc:
        _safe_print(f"[allin] Canh bao: khong doc duoc cookie/token: {exc}")
    return _has_session_cookies() or "x-access-token" in HEADERS


def _save_cookies() -> None:
    data = _session_cookies_dict()
    if not data:
        return
    try:
        COOKIE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _safe_print(f"[allin] Da luu cookie vao: {COOKIE_PATH}")
    except Exception as exc:
        _safe_print(f"[allin] Canh bao: khong luu duoc cookie: {exc}")


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
    rk: Dict[str, Any] = {
        "headers": headers,
        "timeout": kwargs.pop("timeout", TIMEOUT),
        "allow_redirects": kwargs.pop("allow_redirects", True),
    }
    rk.update(kwargs)
    if USE_CURL_CFFI:
        rk.setdefault("impersonate", "chrome110")
    fn = getattr(_SESSION, method.lower())
    try:
        return fn(url, **rk)
    except TypeError as exc:
        if USE_CURL_CFFI and "impersonate" in str(exc).lower():
            rk.pop("impersonate", None)
            return fn(url, **rk)
        raise


# ============================================================
#  Dang nhap
# ============================================================
def login(username: Optional[str] = None, password: Optional[str] = None) -> bool:
    """Dang nhap allin.vn va luu token/cookie."""
    global _LOGIN_ATTEMPTED
    _LOGIN_ATTEMPTED = True

    username = (username or os.getenv("ALLIN_USER") or "").strip()
    password = (password or os.getenv("ALLIN_PASS") or "").strip()

    if not username or not password:
        _safe_print("[allin] Thieu thong tin dang nhap. Dung --user / --pass hoac env ALLIN_USER / ALLIN_PASS.")
        return False

    _safe_print(f"[allin] Dang dang nhap voi tai khoan: {username}...")

    try:
        response = _session_request(
            "POST",
            f"{API_BASE_URL}/api/auth/signin",
            referer=f"{BASE_URL}/auth/login",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"username": username, "password": password},
        )
        status_code = getattr(response, "status_code", 0) or 0
        data = _response_json(response)
        
        if 200 <= status_code < 300 and data.get("token"):
            token = data.get("token")
            # Cap nhat header
            HEADERS["x-access-token"] = token
            
            # Luu token vao file JSON
            token_data = {"x-access-token": token}
            try:
                COOKIE_PATH.write_text(json.dumps(token_data, ensure_ascii=False, indent=2), encoding="utf-8")
                _safe_print(f"[allin] Dang nhap thanh cong, da luu token vao: {COOKIE_PATH}")
            except Exception as exc:
                _safe_print(f"[allin] Canh bao: khong luu duoc token: {exc}")
            return True
        else:
            msg = data.get("message") or data.get("error") or f"HTTP {status_code}"
            _safe_print(f"[allin] Dang nhap that bai: {msg}")
    except Exception as exc:
        _safe_print(f"[allin] Loi dang nhap: {exc}")

    return False


def _maybe_login(force: bool = False) -> bool:
    global _LOGIN_ATTEMPTED
    had_cookie = _load_cookies_once()
    if had_cookie and not force:
        return True
    if _LOGIN_ATTEMPTED:
        return _has_session_cookies()
    user = os.getenv("ALLIN_USER") or ""
    pwd  = os.getenv("ALLIN_PASS") or ""
    if user and pwd:
        return login(user, pwd)
    return had_cookie


# ============================================================
#  Fetch HTML
# ============================================================
def _decode_html(content: bytes, response=None) -> str:
    enc = "utf-8"
    if response is not None:
        ct = getattr(response, "headers", {}).get("content-type", "")
        m = re.search(r"charset=([\w\-]+)", ct, flags=re.I)
        if m:
            enc = m.group(1)
    return content.decode(enc, errors="replace")


def _fetch_html(url: str, tries: int = 3, *, referer: Optional[str] = None) -> BeautifulSoup:
    url = _ensure_url(url)
    if _looks_like_local_file(url):
        content = Path(url).read_bytes()
        return BeautifulSoup(_decode_html(content), "html.parser")
    _maybe_login()
    last_error: Optional[Exception] = None
    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(1.5 * attempt)
        try:
            response = _session_request("GET", url, referer=referer or BASE_URL)
            sc = getattr(response, "status_code", 200)
            if sc == 429:
                _safe_print(f"[allin] Rate-limit, doi {RATE_LIMIT_COOLDOWN}s...")
                time.sleep(RATE_LIMIT_COOLDOWN)
                continue
            if sc in RETRY_STATUS and attempt < tries:
                _safe_print(f"[allin] HTTP {sc}, thu lai ({attempt}/{tries})...")
                continue
            if sc != 200:
                raise FetchHtmlError(f"HTTP {sc}: {url}", sc)
            content = getattr(response, "content", b"")
            if not content:
                content = getattr(response, "text", "").encode("utf-8", errors="replace")
            return BeautifulSoup(_decode_html(content, response), "html.parser")
        except FetchHtmlError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= tries:
                break
    raise FetchHtmlError(f"Khong tai duoc HTML: {url} ({last_error})")


# ============================================================
#  Phan tich URL
# ============================================================
def _story_slug_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0].lower() == "truyen":
        return parts[1]
    return ""


def _story_url_from_any_url(url: str) -> str:
    if _looks_like_local_file(url):
        return url
    parsed = urlparse(url or "")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0].lower() == "truyen":
        return f"{BASE_URL}/truyen/{parts[1]}"
    return url


def _chapter_number_from_url(url: str) -> Optional[int]:
    parsed = urlparse(url or "")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 3 and parts[-1].isdigit():
        try:
            return int(parts[-1])
        except ValueError:
            return None
    return None


def _make_chapter_url(story_url: str, number: int) -> str:
    return story_url.rstrip("/") + f"/{number}"


# ============================================================
#  Trang info truyen
# ============================================================
def _get_chapter_list_from_soup(soup: BeautifulSoup, story_url: str) -> List[Dict[str, str]]:
    """Lay danh sach chuong tu trang info truyen allin.vn."""
    chapters: Dict[int, Dict[str, str]] = {}
    slug = _story_slug_from_url(story_url)
    if not slug:
        return []
    pattern = re.compile(rf"/truyen/{re.escape(slug)}/(\d+)$", re.I)

    # 1. Muc danh sach chuong chinh (class scroll)
    scroll_div = soup.select_one(".scroll")
    search_in = scroll_div or soup
    for a in search_in.find_all("a", href=pattern):
        href = a.get("href", "")
        m = pattern.search(href)
        if not m:
            continue
        num = int(m.group(1))
        span = a.find("span")
        raw_title = _clean_spaces(_text(span) if span else _text(a))
        chap_title = raw_title or f"Chuong {num}"
        full_url = f"{BASE_URL}/truyen/{slug}/{num}"
        chapters.setdefault(num, {"title": chap_title, "url": full_url})

    # 2. Fallback: dropdown chuong
    if not chapters:
        for a in soup.find_all("a", href=pattern):
            href = a.get("href", "")
            m = pattern.search(href)
            if not m:
                continue
            num = int(m.group(1))
            raw_title = _clean_spaces(_text(a)) or f"Chuong {num}"
            full_url = f"{BASE_URL}/truyen/{slug}/{num}"
            chapters.setdefault(num, {"title": raw_title, "url": full_url})

    return [chapters[k] for k in sorted(chapters)]


def _get_book_info(soup: BeautifulSoup, page_url: str) -> Dict[str, Any]:
    """Trich xuat metadata tu trang info truyen allin.vn."""
    # Tieu de
    og_title_tag = soup.find("meta", property="og:title")
    og_title = _clean_spaces(og_title_tag.get("content", "")) if og_title_tag else ""
    title_tag = soup.find("title")
    raw_title = _clean_spaces(_text(title_tag)) if title_tag else og_title
    title = re.sub(r"\s*-\s*.+$", "", raw_title).strip() or raw_title
    h3 = soup.select_one(".right h3")
    if h3:
        t = _clean_spaces(_text(h3))
        if t:
            title = t

    # Tac gia
    author = ""
    author_meta = soup.find("meta", property="book:author")
    if author_meta:
        author = _clean_spaces(author_meta.get("content", ""))
    if not author:
        for p_tag in soup.select(".right p"):
            txt = _text(p_tag)
            if "T\u00e1c gi\u1ea3" in txt or "Tac gia" in txt:
                a_tag = p_tag.find_next("a")
                if a_tag:
                    author = _clean_spaces(_text(a_tag))
                break

    # The loai
    categories: List[str] = []
    for a in soup.select(".right .d-flex.flex-wrap a"):
        href = a.get("href", "")
        if "truyenchu" in href or "name=" in href:
            cat = _clean_spaces(_text(a)).rstrip(",")
            if cat:
                categories.append(cat)

    # Trang thai
    status = ""
    for a in soup.select(".right .d-flex a"):
        href = a.get("href", "")
        if "category" in href and "loading" in href:
            s = _clean_spaces(_text(a))
            if s:
                status = s
                break

    # Gioi thieu
    intro = ""
    intro_node = soup.select_one("[style*='white-space:pre-line']")
    if intro_node:
        intro = intro_node.get_text("\n", strip=False).strip()
    if not intro:
        og_desc = soup.find("meta", property="og:description")
        if og_desc:
            intro = _clean_spaces(og_desc.get("content", ""))

    # Cover URL
    cover_url = ""
    og_img = soup.find("meta", property="og:image")
    if og_img:
        cover_url = _clean_spaces(og_img.get("content", ""))
    if cover_url and not re.match(r"^https?://", cover_url):
        cover_url = urljoin(BASE_URL, cover_url)

    story_url = _story_url_from_any_url(page_url)
    chapters_list = _get_chapter_list_from_soup(soup, story_url)
    total = len(chapters_list)

    # Neu rong, thu doc "Do dai: N"
    if not chapters_list:
        text = soup.get_text(" ", strip=True)
        m = re.search(r"\u0110\u1ed9 d\u00e0i[:\s]+(\d+)", text)
        if not m:
            m = re.search(r"Do dai[:\s]+(\d+)", text, re.I)
        if not m:
            m = re.search(r"(\d+)\s+chu\u01a1ng", text, re.I)
        if m:
            total = int(m.group(1))
            slug = _story_slug_from_url(story_url)
            chapters_list = [
                {"title": f"Chu\u01a1ng {i}", "url": f"{BASE_URL}/truyen/{slug}/{i}"}
                for i in range(1, total + 1)
            ]

    return {
        "title": title or "Truyen",
        "author": author or "Unknown",
        "status": status,
        "category": ", ".join(categories),
        "genre": categories,
        "intro": intro,
        "cover_url": cover_url,
        "url": story_url,
        "total_chapters": len(chapters_list),
        "chapters": chapters_list,
    }


# ============================================================
#  Trang chuong
# ============================================================
def _normalize_chapter_title(title: str) -> str:
    title = _clean_spaces(title)
    m = re.match(
        r"^(?:CHUONG|Chuong|CHU\u01aENG|Ch\u01b0\u01a1ng|CHƯƠNG|chương)\s*(\d+)\s*[:\-\u2013]?\s*(.*)$",
        title, re.UNICODE | re.IGNORECASE,
    )
    if m:
        num  = m.group(1)
        rest = (m.group(2) or "").strip()
        return f"Ch\u01b0\u01a1ng {num}: {rest}" if rest else f"Ch\u01b0\u01a1ng {num}"
    return title


def _fetch_chapter(url: str, fallback_title: str = "") -> Dict[str, Any]:
    """Tai va phan tich mot trang chuong allin.vn."""
    soup = _fetch_html(url, tries=CHAPTER_RETRIES, referer=BASE_URL)

    # Tieu de chuong
    title = ""
    title_node = soup.select_one("div.title")
    if title_node:
        title = _clean_spaces(_text(title_node))
    if not title:
        og_title = soup.find("meta", property="og:title")
        if og_title:
            t = _clean_spaces(og_title.get("content", ""))
            t = re.sub(r"\s*-\s*TRUYEN.*$", "", t, flags=re.I).strip()
            t = re.sub(r"\s*-\s*TRUY\u1ec6N.*$", "", t, flags=re.I).strip()
            title = t
    if not title:
        title_tag = soup.find("title")
        if title_tag:
            t = _clean_spaces(_text(title_tag))
            t = re.sub(r"\s*-\s*TRUYEN.*$", "", t, flags=re.I).strip()
            t = re.sub(r"\s*-\s*TRUY\u1ec6N.*$", "", t, flags=re.I).strip()
            title = t

    title = _normalize_chapter_title(title or fallback_title or "Chapter")

    # Noi dung chuong: div.content.contentabc
    content_node = (
        soup.select_one("div.content.contentabc")
        or soup.select_one("div.contentabc")
        or soup.select_one("div.content")
    )
    if content_node:
        for tag in content_node.find_all(["script", "style", "noscript"]):
            tag.decompose()
        content_html = str(content_node)
    else:
        content_html = "<p>(Khong co noi dung)</p>"

    return {"title": title, "content_html": content_html, "url": url}


# ============================================================
#  Cover
# ============================================================
def _download_cover(cover_url: str) -> Tuple[Optional[bytes], str]:
    if not cover_url:
        return None, ".jpg"
    try:
        _safe_print(f"[allin] Dang tai cover: {cover_url}")
        resp = _session_request("GET", cover_url, referer=BASE_URL, timeout=30)
        sc = getattr(resp, "status_code", 200)
        if sc != 200:
            _safe_print(f"[allin] Canh bao: tai cover that bai HTTP {sc}")
            return None, ".jpg"
        cover_bytes = getattr(resp, "content", b"")
        if not cover_bytes:
            return None, ".jpg"
        url_path = urlparse(cover_url).path
        ext = Path(url_path).suffix.lower() or ".jpg"
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in (".jpg", ".png", ".webp", ".gif"):
            ext = ".jpg"
        if HAS_PILLOW:
            cover_bytes, ext = _process_cover(cover_bytes, ext)
        return cover_bytes, ext
    except Exception as exc:
        _safe_print(f"[allin] Canh bao: loi tai cover: {exc}")
        return None, ".jpg"


def _process_cover(cover_bytes: bytes, ext: str) -> Tuple[bytes, str]:
    """Dung Pillow de resize va convert cover cho EPUB."""
    try:
        img = Image.open(io.BytesIO(cover_bytes))
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            if img.mode in ("RGBA", "LA"):
                bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        max_w, max_h = MAX_COVER_SIZE
        if w > max_w or h > max_h:
            img.thumbnail((max_w, max_h), Image.LANCZOS)
        target_ext = ".png" if ext == ".png" else ".jpg"
        buf = io.BytesIO()
        if target_ext == ".jpg":
            img.save(buf, format="JPEG", quality=90, optimize=True)
        else:
            img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), target_ext
    except Exception as exc:
        _safe_print(f"[allin] Canh bao: Pillow xu ly anh loi: {exc}")
        return cover_bytes, ext


# ============================================================
#  API cong khai
# ============================================================
def getText(url: str = DEFAULT_URL) -> Dict[str, Any]:
    """Lay thong tin truyen (info + danh sach chuong)."""
    url = _ensure_url(url)
    story_url = _story_url_from_any_url(url)
    _safe_print(f"[allin] Trang info: {story_url}")
    soup = _fetch_html(story_url)
    return _get_book_info(soup, story_url)


def getChapter(url: str) -> Dict[str, Any]:
    """Tai va tra ve noi dung mot chuong."""
    return _fetch_chapter(url)


# ============================================================
#  Build EPUB don gian (fallback)
# ============================================================
def _build_epub_simple(
    epub_path: str,
    book_title: str,
    author: str,
    story_url: str,
    chapters_data: List[Dict],
    cover_bytes: Optional[bytes],
    cover_ext: str,
    genre: Any,
    intro: str,
) -> str:
    import zipfile
    from uuid import uuid4
    from datetime import datetime, timezone

    uid        = f"urn:uuid:{uuid4()}"
    now        = datetime.now(timezone.utc)
    has_cover  = bool(cover_bytes)
    cover_name = f"Images/cover{cover_ext}"
    ext_map    = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    cover_media = ext_map.get(cover_ext, "image/jpeg")

    def xhtml_page(title_str: str, body_str: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi" lang="vi">'
            '<head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>'
            f'<title>{html.escape(title_str)}</title>'
            '<style>body{font-family:serif;line-height:1.75;margin:5%}'
            'h1{text-align:center}p{margin:.65em 0;text-indent:2em}'
            '.cover{text-align:center}.cover img{max-width:100%}</style>'
            f'</head><body>{body_str}</body></html>'
        ).encode("utf-8")

    with zipfile.ZipFile(epub_path, "w") as z:
        def w(name, data, compress=True):
            zi = zipfile.ZipInfo(name)
            zi.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
            z.writestr(zi, data)

        w("mimetype", b"application/epub+zip", compress=False)
        w("META-INF/container.xml", (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"'
            ' media-type="application/oebps-package+xml"/></rootfiles></container>'
        ).encode("utf-8"))

        mf   = ['<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
        sp   = []
        nav  = []
        po   = 1

        if has_cover:
            w("OEBPS/" + cover_name, cover_bytes)
            cb = f'<p class="cover"><img src="../{cover_name}" alt="{html.escape(book_title)}"/></p>'
            w("OEBPS/Text/cover.xhtml", xhtml_page("Cover", cb))
            mf.append(f'<item id="cover-image" href="{cover_name}" media-type="{cover_media}"/>')
            mf.append('<item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
            sp.append('<itemref idref="cover"/>')
            nav.append(
                f'<navPoint id="nav-cover" playOrder="{po}"><navLabel><text>Cover</text></navLabel>'
                '<content src="Text/cover.xhtml"/></navPoint>'
            )
            po += 1

        intro_body = f"<h1>{html.escape(book_title)}</h1>"
        intro_body += f'<p><strong>T\u00e1c gi\u1ea3:</strong> {html.escape(author)}</p>'
        if intro:
            intro_body += "<hr/>"
            for line in intro.splitlines():
                line = line.strip()
                if line:
                    intro_body += f"<p>{html.escape(line)}</p>"
        w("OEBPS/Text/title.xhtml", xhtml_page(book_title, intro_body))
        mf.append('<item id="titlepage" href="Text/title.xhtml" media-type="application/xhtml+xml"/>')
        sp.append('<itemref idref="titlepage"/>')
        nav.append(
            f'<navPoint id="nav-title" playOrder="{po}"><navLabel><text>Gi\u1edbi thi\u1ec7u</text></navLabel>'
            '<content src="Text/title.xhtml"/></navPoint>'
        )
        po += 1

        for i, c in enumerate(chapters_data, 1):
            fn   = f"Text/chapter_{i:04d}.xhtml"
            body = f"<h1>{html.escape(c['title'])}</h1>\n{c.get('content_html') or '<p>(No content)</p>'}"
            w("OEBPS/" + fn, xhtml_page(c["title"], body))
            mf.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            sp.append(f'<itemref idref="chap{i}"/>')
            nav.append(
                f'<navPoint id="nav{i}" playOrder="{po}">'
                f'<navLabel><text>{html.escape(c["title"])}</text></navLabel>'
                f'<content src="{fn}"/></navPoint>'
            )
            po += 1

        genres_xml = ""
        if isinstance(genre, list):
            for g in genre:
                genres_xml += f"    <dc:subject>{html.escape(str(g))}</dc:subject>\n"
        cover_meta = '<meta name="cover" content="cover-image"/>' if has_cover else ""
        cover_guide = (
            '<guide><reference type="cover" title="Cover" href="Text/cover.xhtml"/></guide>'
            if has_cover else ""
        )

        opf = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="BookId">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">'
            f'<dc:identifier id="BookId">{html.escape(uid)}</dc:identifier>'
            f'<dc:title>{html.escape(book_title)}</dc:title>'
            f'<dc:creator opf:role="aut">{html.escape(author)}</dc:creator>'
            '<dc:publisher>Hishiro</dc:publisher>'
            f'{genres_xml}'
            '<dc:language>vi</dc:language>'
            f'<dc:source>{html.escape(story_url)}</dc:source>'
            f'<dc:date>{now.strftime("%Y-%m-%d")}</dc:date>'
            f'{cover_meta}'
            '</metadata>'
            f'<manifest>{"".join(mf)}</manifest>'
            f'<spine toc="ncx">{"".join(sp)}</spine>'
            f'{cover_guide}'
            '</package>'
        ).encode("utf-8")
        w("OEBPS/content.opf", opf)

        toc = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            '<head>'
            f'<meta name="dtb:uid" content="{html.escape(uid)}"/>'
            '<meta name="dtb:depth" content="1"/>'
            '<meta name="dtb:totalPageCount" content="0"/>'
            '<meta name="dtb:maxPageNumber" content="0"/>'
            '</head>'
            f'<docTitle><text>{html.escape(book_title)}</text></docTitle>'
            f'<navMap>{"".join(nav)}</navMap>'
            '</ncx>'
        ).encode("utf-8")
        w("OEBPS/toc.ncx", toc)

    _safe_print(f"[allin] Da tao EPUB: {epub_path}")
    return epub_path


# ============================================================
#  Download pipeline
# ============================================================
def _download_all(
    url: str,
    *,
    out_dir: Optional[str] = None,
    fmt: str = "epub",
    start: int = 1,
    end: Optional[int] = None,
    sleep: float = SLEEP_BETWEEN_CHAPS,
    skip_existing: bool = True,
) -> str:
    info         = getText(url)
    book_title   = info.get("title", "Truyen")
    author       = info.get("author", "Unknown")
    cover_url    = info.get("cover_url", "")
    story_url    = info.get("url", url)
    chapters_all = info.get("chapters", [])
    genre        = info.get("genre", [])
    intro        = info.get("intro", "")

    total    = len(chapters_all)
    s        = max(1, start) - 1
    e        = min(total, end) if end else total
    chapters = chapters_all[s:e]

    _safe_print(f"\n[allin] Truyen  : {book_title}")
    _safe_print(f"[allin] Tac gia : {author}")
    _safe_print(f"[allin] Chuong  : {total} tong | Tai {len(chapters)} chuong ({s+1}-{e})")

    cover_bytes, cover_ext = _download_cover(cover_url)

    slug      = _slugify(book_title)
    cache_dir = Path(out_dir or str(OUTPUT_BASE / slug))
    cache_dir.mkdir(parents=True, exist_ok=True)

    if fmt == "epub":
        epub_path = str(cache_dir / (_safe_filename(book_title) + ".epub"))
    else:
        epub_path = str(cache_dir)

    chapters_data: List[Dict] = []
    for idx_rel, chap in enumerate(chapters, 1):
        idx        = s + idx_rel
        chap_url   = chap["url"]
        chap_title = chap.get("title") or f"Chuong {idx}"
        html_file  = cache_dir / f"{idx:04d} - {_safe_filename(chap_title, 80)}.html"

        if skip_existing and html_file.exists() and html_file.stat().st_size > 100:
            _safe_print(f"[allin] [{idx:04d}/{total}] Cache: {chap_title}")
            try:
                with html_file.open("r", encoding="utf-8") as f:
                    cached = f.read()
                soup_c = BeautifulSoup(cached, "html.parser")
                body   = soup_c.find("body")
                chapters_data.append({
                    "title": chap_title,
                    "content_html": str(body) if body else cached,
                    "url": chap_url,
                })
                continue
            except Exception:
                pass

        _safe_print(f"[allin] [{idx:04d}/{total}] Tai: {chap_title}")
        try:
            c = _fetch_chapter(chap_url, fallback_title=chap_title)
            chapters_data.append(c)
            try:
                with html_file.open("w", encoding="utf-8") as f:
                    f.write(f"<!-- url: {chap_url} -->\n")
                    f.write(
                        f"<html><head><meta charset='utf-8'/>"
                        f"<title>{html.escape(c['title'])}</title></head>"
                        f"<body>{c['content_html']}</body></html>"
                    )
            except Exception as exc:
                _safe_print(f"[allin] Canh bao: khong luu cache: {exc}")
            time.sleep(sleep)
        except Exception as exc:
            _safe_print(f"[allin] Loi tai chuong {idx}: {exc}")
            chapters_data.append({
                "title": chap_title,
                "content_html": f"<p>(Loi: {html.escape(str(exc))})</p>",
                "url": chap_url,
            })
            time.sleep(sleep * 2)

    if fmt != "epub":
        _safe_print(f"\n[allin] HTML da luu vao: {cache_dir}")
        return str(cache_dir)

    _safe_print("\n[allin] Dang dong goi EPUB...")
    try:
        from epub_builder import create_epub
        result = create_epub(
            book_url=story_url,
            book_title=book_title,
            author=author,
            chapters=[{"title": c["title"], "url": c.get("url", "")} for c in chapters_data],
            chapters_data=chapters_data,
            cover_bytes=cover_bytes,
            cover_ext=cover_ext,
            language="vi",
            tags=genre,
            out_epub_path=epub_path,
            book_info={
                "title": book_title,
                "author": author,
                "status": info.get("status", ""),
                "intro": intro,
                "genre": genre,
            },
            intro=intro,
        )
        _safe_print(f"[allin] EPUB: {result}")
        return result
    except ImportError:
        _safe_print("[allin] Khong tim thay epub_builder.py, dung fallback...")
        return _build_epub_simple(
            epub_path=epub_path,
            book_title=book_title,
            author=author,
            story_url=story_url,
            chapters_data=chapters_data,
            cover_bytes=cover_bytes,
            cover_ext=cover_ext,
            genre=genre,
            intro=intro,
        )


# ============================================================
#  CLI
# ============================================================
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="allin",
        description="Tai truyen tu allin.vn va xuat EPUB/HTML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Vi du:\n"
            "  python allin.py                                            # Tuong tac\n"
            "  python allin.py https://allin.vn/truyen/minh-nguyet-do-kiem  # Nhanh\n"
            "  python allin.py <url> --start 1 --end 20 --format html     # HTML 1-20\n"
            "  python allin.py <url> --user HishiroO --pass 821990Miku    # Dang nhap\n"
            "  python allin.py <url> --info                               # Chi info\n"
        ),
    )
    p.add_argument("url", nargs="?", default="")
    p.add_argument("--user",     default="")
    p.add_argument("--pass",     dest="password", default="")
    p.add_argument("--login",    action="store_true")
    p.add_argument("--format",   dest="fmt", choices=["epub", "html"], default="epub")
    p.add_argument("--start",    type=int, default=1)
    p.add_argument("--end",      type=int, default=None)
    p.add_argument("--out",      default="")
    p.add_argument("--delay",    type=float, default=SLEEP_BETWEEN_CHAPS)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--info",     action="store_true")
    return p


def _interactive_mode() -> Tuple[str, str, str, int, Optional[int]]:
    _safe_print("\n" + "=" * 60)
    _safe_print("  ALLIN.VN STORY DOWNLOADER")
    _safe_print("  https://allin.vn")
    _safe_print("=" * 60)
    url = input("URL trang truyen (Enter = mac dinh): ").strip() or DEFAULT_URL
    _safe_print("\nDinh dang xuat: 1=epub  2=html")
    fmt = "html" if input("Chon [1/2]: ").strip() == "2" else "epub"
    start_s = input("Chuong bat dau (Enter=1): ").strip()
    start   = int(start_s) if start_s.isdigit() else 1
    end_s   = input("Chuong ket thuc (Enter=tat ca): ").strip()
    end_val: Optional[int] = int(end_s) if end_s.isdigit() else None
    return url, fmt, "", start, end_val


def main() -> None:
    parser = _build_arg_parser()
    args   = parser.parse_args()

    user = args.user or os.getenv("ALLIN_USER") or ""
    pwd  = args.password or os.getenv("ALLIN_PASS") or ""

    if args.login or (user and pwd):
        if user and pwd:
            login(user, pwd)
        else:
            _safe_print("[allin] --login yeu cau --user va --pass")
    else:
        _load_cookies_once()

    url   = args.url.strip()
    fmt   = args.fmt
    start = args.start
    end   = args.end
    out   = args.out.strip()

    if not url:
        url, fmt, out, start, end = _interactive_mode()

    url = _ensure_url(url)

    if args.info:
        _safe_print(f"\n[allin] Lay info: {url}")
        info = getText(url)
        _safe_print("=" * 50)
        _safe_print(f"  Tieu de   : {info.get('title')}")
        _safe_print(f"  Tac gia   : {info.get('author')}")
        _safe_print(f"  The loai  : {info.get('category')}")
        _safe_print(f"  Trang thai: {info.get('status')}")
        _safe_print(f"  So chuong : {info.get('total_chapters')}")
        _safe_print(f"  Cover     : {info.get('cover_url')}")
        _safe_print("=" * 50)
        intro = info.get("intro", "")
        if intro:
            _safe_print(f"\nGioi thieu:\n{intro[:600]}")
        return

    _safe_print(f"\n[allin] Bat dau tai: {url}")
    result = _download_all(
        url,
        out_dir=out or None,
        fmt=fmt,
        start=start,
        end=end,
        sleep=args.delay,
        skip_existing=not args.no_cache,
    )
    _safe_print(f"\n[allin] Hoan thanh! -> {result}")


if __name__ == "__main__":
    main()
