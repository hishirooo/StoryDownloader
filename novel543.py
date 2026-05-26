# -*- coding: utf-8 -*-
"""
Downloader for https://www.novel543.com/ (Ji Xia Shu Yuan).

Book page sample:
  https://www.novel543.com/0327692090/

Catalog page sample:
  https://www.novel543.com/0327692090/dir

Chapter page sample:
  https://www.novel543.com/0327692090/8096_17.html

Novel543 can show a browser verification page. The normal path uses
requests/curl_cffi; if a verification page or 403 is detected, the module can
fall back to a persistent Playwright browser profile so the site can validate a
real browser session.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import argparse
import atexit
import html
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

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

BASE_URL = "https://www.novel543.com/"
DEFAULT_URL = "https://www.novel543.com/0327692090/"
OUTPUT_BASE = Path("output")
DEFAULT_USER_AGENT = os.environ.get(
    "NOVEL543_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
)

HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,vi;q=0.8,en;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": BASE_URL,
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 30
SLEEP_BETWEEN_PAGES = 0.6
SLEEP_BETWEEN_CHAPS = 0.9
CHAPTER_RETRIES = 5
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)
MAX_CHAPTER_PARTS = 20
BROWSER_VERIFY_TIMEOUT = int(os.environ.get("NOVEL543_VERIFY_TIMEOUT", "90"))
COOKIE_FILE = Path(os.environ.get("NOVEL543_COOKIE_FILE", "novel543_cookie.txt"))

_PW = None
_BROWSER_CONTEXT = None
_COOKIE_HEADER_OVERRIDE: Optional[str] = None
_USE_CLOAK_BROWSER = False
_CLOAK_PROFILE_DIR = Path(os.environ.get("NOVEL543_CLOAK_PROFILE", "playwright_profile/novel543_cloak"))
_CLOAK_DISABLE_HTTP2 = os.environ.get("NOVEL543_CLOAK_DISABLE_HTTP2", "0") == "1"
_MANUAL_VERIFY_ENABLED = os.environ.get("NOVEL543_MANUAL_VERIFY", "0") == "1"
_MANUAL_VERIFY_PROFILE = Path(os.environ.get("NOVEL543_MANUAL_PROFILE", "playwright_profile/novel543_real_browser"))
_MANUAL_VERIFY_PORT = int(os.environ.get("NOVEL543_MANUAL_PORT", "9222"))
_MANUAL_VERIFY_TIMEOUT = int(os.environ.get("NOVEL543_MANUAL_TIMEOUT", "240"))
_MANUAL_VERIFY_BROWSER = os.environ.get("NOVEL543_MANUAL_BROWSER", "")
_MANUAL_VERIFY_DONE = False
_MANUAL_VERIFY_CLOSE_BROWSER = os.environ.get("NOVEL543_MANUAL_KEEP_OPEN", "0") != "1"
_MANUAL_BROWSER_PROCESS: Optional[subprocess.Popen] = None

AUTHOR_LABEL = "\u4f5c\u8005"
CATEGORY_LABELS = ("\u5206\u985e", "\u5206\u7c7b")
UPDATE_LABEL = "\u66f4\u65b0"
CHAPTERS_LABELS = ("\u7ae0\u7bc0", "\u7ae0\u8282")
CATALOG_LABELS = ("\u76ee\u9304", "\u76ee\u5f55")
CHAPTER_LIST_LABELS = ("\u7ae0\u7bc0\u5217\u8868", "\u7ae0\u8282\u5217\u8868")
ONLINE_READ_LABELS = ("\u5c0f\u8aaa\u5728\u7dda\u95b1\u8b80", "\u5c0f\u8bf4\u5728\u7ebf\u9605\u8bfb")
SITE_NAME = "\u7a37\u4e0b\u66f8\u9662"


class FetchHtmlError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class BytesResponse:
    def __init__(self, content: bytes, status_code: int, headers: Optional[Dict[str, str]] = None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise FetchHtmlError(f"HTTP={self.status_code}", self.status_code)


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
        value = re.sub(rf"^{re.escape(label)}\s*[:\uff1a/]?\s*", "", value).strip()
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

def _normalize_cookie_header(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""

    lines = [line.strip() for line in raw.replace("\r\n", "\n").split("\n") if line.strip()]
    for line in lines:
        if line.lower().startswith("cookie:"):
            return line.split(":", 1)[1].strip()

    # Netscape cookies.txt format: domain, flag, path, secure, expiry, name, value.
    if any("\t" in line for line in lines):
        pairs = []
        for line in lines:
            if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                name = parts[-2].strip()
                value = parts[-1].strip()
                if name:
                    pairs.append(f"{name}={value}")
        if pairs:
            return "; ".join(pairs)

    if len(lines) > 1:
        pairs = []
        for line in lines:
            if line.startswith("#"):
                continue
            if ";" in line:
                pairs.extend(part.strip() for part in line.split(";") if "=" in part)
            elif "=" in line:
                pairs.append(line)
        if pairs:
            return "; ".join(pairs)
    return raw

def _read_cookie_header_from_file(path: Optional[Path] = None) -> str:
    path = path or COOKIE_FILE
    try:
        if path.exists():
            return _normalize_cookie_header(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return ""
    return ""

def _cookie_header() -> str:
    if _COOKIE_HEADER_OVERRIDE:
        return _COOKIE_HEADER_OVERRIDE
    env_cookie = _normalize_cookie_header(os.environ.get("NOVEL543_COOKIE", ""))
    if env_cookie:
        return env_cookie
    return _read_cookie_header_from_file()

def _configure_cookie(cookie: str = "", cookie_file: Optional[str] = None) -> None:
    global _COOKIE_HEADER_OVERRIDE, COOKIE_FILE
    if cookie_file:
        COOKIE_FILE = Path(cookie_file)
    if cookie:
        _COOKIE_HEADER_OVERRIDE = _normalize_cookie_header(cookie)
    elif cookie_file:
        _COOKIE_HEADER_OVERRIDE = _read_cookie_header_from_file(COOKIE_FILE)

def _configure_user_agent(user_agent: str = "") -> None:
    user_agent = (user_agent or "").strip()
    if user_agent:
        HEADERS["User-Agent"] = user_agent

def _configure_cloak_browser(enabled: bool = False, profile_dir: str = "", disable_http2: bool = False) -> None:
    global _USE_CLOAK_BROWSER, _CLOAK_PROFILE_DIR, _CLOAK_DISABLE_HTTP2
    _USE_CLOAK_BROWSER = bool(enabled)
    if profile_dir:
        _CLOAK_PROFILE_DIR = Path(profile_dir)
    if disable_http2:
        _CLOAK_DISABLE_HTTP2 = True

def _configure_manual_verify(
    enabled: bool = False,
    profile_dir: str = "",
    port: Optional[int] = None,
    timeout: Optional[int] = None,
    browser_path: str = "",
    keep_open: bool = False,
) -> None:
    global _MANUAL_VERIFY_ENABLED, _MANUAL_VERIFY_PROFILE, _MANUAL_VERIFY_PORT, _MANUAL_VERIFY_TIMEOUT, _MANUAL_VERIFY_BROWSER, _MANUAL_VERIFY_CLOSE_BROWSER
    _MANUAL_VERIFY_ENABLED = bool(enabled)
    if profile_dir:
        _MANUAL_VERIFY_PROFILE = Path(profile_dir)
    if port:
        _MANUAL_VERIFY_PORT = int(port)
    if timeout:
        _MANUAL_VERIFY_TIMEOUT = int(timeout)
    if browser_path:
        _MANUAL_VERIFY_BROWSER = browser_path
    _MANUAL_VERIFY_CLOSE_BROWSER = not keep_open

def _cookies_for_browser_context() -> List[Dict[str, object]]:
    cookie_header = _cookie_header()
    cookies: List[Dict[str, object]] = []
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append({"name": name, "value": value.strip(), "domain": ".novel543.com", "path": "/"})
    return cookies


def _browser_executable_path() -> Optional[str]:
    candidates = [
        _MANUAL_VERIFY_BROWSER,
        os.environ.get("NOVEL543_BROWSER"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None

def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.35):
            return True
    except OSError:
        return False

def _wait_for_cdp(port: int, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_is_open(port):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as response:
                    if response.status == 200:
                        return True
            except Exception:
                pass
        time.sleep(0.5)
    return False

def _launch_real_browser_for_verify(url: str, port: int, profile_dir: Path) -> subprocess.Popen:
    global _MANUAL_BROWSER_PROCESS
    executable_path = _browser_executable_path()
    if not executable_path:
        raise RuntimeError("Khong tim thay Chrome/Edge that de mo manual verify")

    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        executable_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        url,
    ]
    _MANUAL_BROWSER_PROCESS = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return _MANUAL_BROWSER_PROCESS

def _close_manual_browser(browser=None, launched_by_us: bool = False) -> None:
    global _MANUAL_BROWSER_PROCESS
    if not (_MANUAL_VERIFY_CLOSE_BROWSER and launched_by_us):
        return
    try:
        if browser is not None:
            browser.close()
            _safe_print("Da dong Chrome/Edge manual verify.")
            return
    except Exception:
        pass
    process = _MANUAL_BROWSER_PROCESS
    if process is not None:
        try:
            process.terminate()
            _safe_print("Da dong tien trinh Chrome/Edge manual verify.")
        except Exception:
            pass
        _MANUAL_BROWSER_PROCESS = None

def _html_is_usable_novel_page(markup: str) -> bool:
    if not markup or _looks_like_challenge_html(markup):
        return False
    soup = BeautifulSoup(markup, "html.parser")
    return bool(
        soup.select_one("#detail h1.title")
        or soup.select_one(".chaplist ul.all a[href]")
        or soup.select_one(".chapter-content .content")
        or _meta_content(soup, "og:novel:book_name")
    )

def _cookies_to_header(cookies: List[Dict[str, object]]) -> str:
    pairs = []
    seen = set()
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if "novel543.com" not in domain:
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)

def _manual_verify_with_real_browser(url: str) -> bool:
    global _COOKIE_HEADER_OVERRIDE, _MANUAL_VERIFY_DONE

    url = _ensure_url(url)
    port = _MANUAL_VERIFY_PORT
    profile_dir = _MANUAL_VERIFY_PROFILE
    launched_by_us = False
    verified = False
    if not _port_is_open(port):
        _safe_print(f"Mo Chrome/Edge that de xac minh Novel543 (port {port})...")
        _launch_real_browser_for_verify(url, port, profile_dir)
        launched_by_us = True
        if not _wait_for_cdp(port, timeout=25):
            raise RuntimeError("Khong ket noi duoc DevTools cua Chrome/Edge manual verify")
    else:
        _safe_print(f"Dang dung Chrome/Edge manual verify dang mo tren port {port}...")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Can cai playwright de doc cookie tu Chrome/Edge manual verify") from exc

    deadline = time.time() + max(30, _MANUAL_VERIFY_TIMEOUT)
    _safe_print("Hay cho trang Novel543 tai xong trong browser vua mo. Script se tu nhan dien va lay cookie.")
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        target_page = None
        for page in context.pages:
            if "novel543.com" in (page.url or ""):
                target_page = page
                break
        if target_page is None:
            target_page = context.new_page()
            target_page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)

        while time.time() < deadline:
            for page in context.pages:
                if "novel543.com" in (page.url or ""):
                    target_page = page
                    break
            try:
                markup = target_page.content() if target_page else ""
                if _html_is_usable_novel_page(markup):
                    try:
                        browser_ua = target_page.evaluate("() => navigator.userAgent")
                        if browser_ua:
                            HEADERS["User-Agent"] = str(browser_ua)
                    except Exception:
                        pass
                    cookies = context.cookies("https://www.novel543.com/")
                    cookie_header = _cookies_to_header(cookies)
                    if cookie_header:
                        _COOKIE_HEADER_OVERRIDE = cookie_header
                        try:
                            COOKIE_FILE.write_text(cookie_header, encoding="utf-8")
                            _safe_print(f"Da luu cookie Novel543 vao: {COOKIE_FILE}")
                        except OSError:
                            pass
                    _MANUAL_VERIFY_DONE = True
                    verified = True
                    _safe_print("Da nhan dien trang hop le sau xac minh, tiep tuc tai...")
                    _close_manual_browser(browser, launched_by_us=launched_by_us)
                    return True
            except Exception:
                pass
            time.sleep(1.0)
        return False
    finally:
        try:
            pw.stop()
        except Exception:
            pass
        if not verified:
            _close_manual_browser(None, launched_by_us=launched_by_us)

def _manual_verify_if_enabled(url: str, *, force: bool = False) -> bool:
    global _MANUAL_VERIFY_DONE
    if not _MANUAL_VERIFY_ENABLED:
        return False
    if force:
        _MANUAL_VERIFY_DONE = False
    if _MANUAL_VERIFY_DONE and _cookie_header():
        return True
    return _manual_verify_with_real_browser(url)


def _close_browser_context() -> None:
    global _PW, _BROWSER_CONTEXT
    _close_manual_browser(None, launched_by_us=True)
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

    if _USE_CLOAK_BROWSER:
        try:
            from cloakbrowser import launch_persistent_context
        except ImportError as exc:
            raise RuntimeError(
                "Chua cai cloakbrowser. Cai theo huong dan cua CloakHQ/CloakBrowser roi chay lai voi --cloak."
            ) from exc

        _CLOAK_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        launch_args = []
        if _CLOAK_DISABLE_HTTP2:
            launch_args.append("--disable-http2")
        _BROWSER_CONTEXT = launch_persistent_context(
            str(_CLOAK_PROFILE_DIR),
            headless=False,
            locale="zh-TW",
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={
                "Accept-Language": HEADERS["Accept-Language"],
                "Upgrade-Insecure-Requests": "1",
            },
            args=launch_args or None,
        )
        cookies = _cookies_for_browser_context()
        if cookies:
            try:
                _BROWSER_CONTEXT.add_cookies(cookies)
            except Exception:
                pass
        return _BROWSER_CONTEXT

    from playwright.sync_api import sync_playwright

    executable_path = _browser_executable_path()
    if not executable_path:
        raise RuntimeError("Khong tim thay Chrome/Edge de xu ly xac minh Novel543")

    _PW = sync_playwright().start()
    profile_dir = Path("playwright_profile") / "novel543"
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = os.environ.get("NOVEL543_BROWSER_HEADLESS", "0") == "1"
    _BROWSER_CONTEXT = _PW.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        executable_path=executable_path,
        headless=headless,
        locale="zh-TW",
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
    cookies = _cookies_for_browser_context()
    if cookies:
        try:
            _BROWSER_CONTEXT.add_cookies(cookies)
        except Exception:
            pass
    return _BROWSER_CONTEXT


def _looks_like_challenge_html(markup: str) -> bool:
    lowered = (markup or "").lower()
    markers = [
        "cf-chl",
        "challenge-platform",
        "turnstile",
        "checking if the site connection is secure",
        "ray id",
        "challenges.cloudflare.com",
        "\u0054h\u1ef1c hi\u1ec7n x\u00e1c minh b\u1ea3o m\u1eadt".lower(),
        "\u6b63\u5728\u57f7\u884c\u5b89\u5168\u9a57\u8b49",
        "\u700f\u89bd\u5668\u64f4\u5145\u529f\u80fd\u6216\u7db2\u8def\u8a2d\u5b9a\u4e0d\u76f8\u5bb9",
        "\u5be6\u65bd\u9a57\u8b49",
        "\u5b89\u5168\u9a57\u8b49",
    ]
    return any(marker.lower() in lowered for marker in markers)


def _looks_like_challenge_soup(soup: BeautifulSoup) -> bool:
    if not soup:
        return False
    title = _text(soup.find("title"))
    body_text = soup.get_text(" ", strip=True)[:4000]
    return _looks_like_challenge_html(f"{title}\n{body_text}\n{str(soup)[:4000]}")


def _browser_get(url: str, referer: Optional[str] = None) -> BytesResponse:
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
            page.wait_for_load_state("networkidle", timeout=7000)
        except Exception:
            pass

        deadline = time.time() + max(5, BROWSER_VERIFY_TIMEOUT)
        while time.time() < deadline:
            markup = page.content()
            if not _looks_like_challenge_html(markup):
                break
            page.wait_for_timeout(1000)

        status_code = response.status if response else 0
        content = page.content().encode("utf-8", errors="replace")
        return BytesResponse(content, status_code or 200, {"content-type": "text/html; charset=utf-8"})
    finally:
        try:
            page.close()
        except Exception:
            pass


def _http_get(url: str, referer: Optional[str] = None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    cookie_header = _cookie_header()
    if cookie_header:
        headers["Cookie"] = cookie_header
    kwargs = {"headers": headers, "timeout": TIMEOUT}
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


def _response_soup(response) -> BeautifulSoup:
    content = getattr(response, "content", b"")
    if not content:
        text = getattr(response, "text", "")
        content = text.encode(getattr(response, "encoding", "utf-8") or "utf-8", errors="replace")
    return BeautifulSoup(_decode_html(content, response), "html.parser")


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
            soup = _response_soup(response)

            if status_code == 403 or _looks_like_challenge_soup(soup):
                if _manual_verify_if_enabled(url, force=True):
                    response = _http_get(url, referer=referer)
                    status_code = getattr(response, "status_code", 200)
                    last_status = status_code
                    soup = _response_soup(response)
                    if status_code < 400 and not _looks_like_challenge_soup(soup):
                        return soup, status_code

                if _cookie_header():
                    raise FetchHtmlError(
                        "Novel543 van dang yeu cau xac minh. Cookie co the da het han hoac khong khop browser/IP.",
                        last_status,
                    )
                try:
                    _safe_print("Novel543 yeu cau xac minh, dang mo browser fallback...")
                    response = _browser_get(url, referer=referer)
                    status_code = getattr(response, "status_code", 200)
                    last_status = status_code
                    soup = _response_soup(response)
                except Exception as browser_exc:
                    raise FetchHtmlError(
                        "Novel543 dang yeu cau xac minh. Cai playwright hoac mo Chrome/Edge "
                        f"de xac minh mot lan: {browser_exc}",
                        last_status,
                    ) from browser_exc

            if _looks_like_challenge_soup(soup):
                raise FetchHtmlError(
                    "Novel543 van dang o man hinh xac minh. Hay lay Cookie tu Chrome/Edge that va luu vao novel543_cookie.txt.",
                    last_status,
                )

            if status_code in RETRY_STATUS and attempt < tries:
                continue
            response.raise_for_status()
            return soup, status_code
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


def _book_id_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path
    match = re.search(r"/(?P<book>\d{4,})(?:/|$)", path)
    return match.group("book") if match else None


def _book_id_from_soup(soup: BeautifulSoup) -> Optional[str]:
    for a in soup.select("a[href]"):
        book_id = _book_id_from_url(a.get("href", ""))
        if book_id:
            return book_id
    return None


def _book_url_from_id(book_id: str) -> str:
    return f"{BASE_URL}{book_id}/"


def _catalog_url_from_book_id(book_id: str) -> str:
    return f"{BASE_URL}{book_id}/dir"


def _chapter_match(url: str):
    return re.search(
        r"/(?P<book>\d{4,})/(?P<sid>\d+)_(?P<chapter>\d+)(?:_(?P<part>\d+))?\.html?$",
        urlparse(url).path,
        flags=re.I,
    )


def _chapter_identity_from_url(url: str) -> Optional[str]:
    match = _chapter_match(url)
    if match:
        return f"{match.group('book')}:{match.group('sid')}:{match.group('chapter')}"
    match = re.search(r"/(?P<book>\d{4,})/(?P<chapter>\d+)\.html?$", urlparse(url).path, flags=re.I)
    if match:
        return f"{match.group('book')}:_:{match.group('chapter')}"
    return None


def _chapter_number_from_url(url: str) -> Optional[int]:
    match = _chapter_match(url)
    if match:
        return int(match.group("chapter"))
    match = re.search(r"/(\d+)\.html?$", urlparse(url).path, flags=re.I)
    return int(match.group(1)) if match else None


def _first_part_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"(/\d{4,}/\d+_\d+)(?:_\d+)?\.html?$", r"\1.html", parsed.path, flags=re.I)
    return parsed._replace(path=path, query="", fragment="").geturl()


def _base_chapter_url(url: str) -> str:
    return _first_part_url(_normalized_url(url))


def _is_chapter_url(page_url: str, chapter_url: str, *, allow_part: bool = False) -> bool:
    page = urlparse(page_url if urlparse(page_url).scheme else BASE_URL)
    target = urlparse(chapter_url)
    if target.netloc and page.netloc and target.netloc != page.netloc:
        return False
    page_book_id = _book_id_from_url(page_url)
    target_book_id = _book_id_from_url(chapter_url)
    if not target_book_id:
        return False
    if page_book_id and target_book_id != page_book_id:
        return False
    basename = target.path.rsplit("/", 1)[-1]
    if allow_part:
        return bool(re.fullmatch(r"(?:\d+_\d+|\d+)(?:_\d+)?\.html?", basename, flags=re.I))
    return bool(re.fullmatch(r"(?:\d+_\d+|\d+)\.html?", basename, flags=re.I))


def _is_catalog_url(url: str) -> bool:
    return bool(_book_id_from_url(url) and re.search(r"/\d{4,}/dir/?$", urlparse(url).path, flags=re.I))


def _chapter_referer(url: str) -> str:
    book_id = _book_id_from_url(url)
    return _book_url_from_id(book_id) if book_id else BASE_URL


def _field_from_spans(soup: BeautifulSoup, *labels: str) -> str:
    for node in soup.select("#detail .meta span, .meta-dir span, .info .meta span"):
        text = _clean_spaces(node.get_text(" ", strip=True))
        compact = re.sub(r"\s+", "", text)
        for label in labels:
            if compact.startswith(label):
                return _strip_label(text, label)
    return ""


def _clean_book_title(title: str) -> str:
    title = _clean_spaces(title)
    match = re.search(r"\u300a([^\u300b]+)\u300b", title)
    if match:
        title = match.group(1)
    for marker in CHAPTER_LIST_LABELS + ONLINE_READ_LABELS + (SITE_NAME,):
        title = title.split(marker, 1)[0].strip(" -_|")
    title = re.sub(r"\(\s*[^)]*\s*\)\s*$", "", title).strip()
    return title.strip(" \u300a\u300b-_")


def _find_latest_node(soup: BeautifulSoup, page_url: str):
    for ul in soup.select(".chaplist ul"):
        classes = set(ul.get("class") or [])
        if "all" in classes:
            continue
        for a in ul.select("a[href]"):
            url = _absolute_url(page_url, a.get("href", ""))
            if _is_chapter_url(page_url, url, allow_part=True):
                return a
    for a in soup.select(".chaplist a[href]"):
        url = _absolute_url(page_url, a.get("href", ""))
        if _is_chapter_url(page_url, url, allow_part=True):
            return a
    return None


def _find_catalog_url(soup: BeautifulSoup, page_url: str) -> str:
    for selector in [
        "a[href$='/dir']",
        "a[href*='/dir']",
        ".more a[href]",
        ".foot-nav a[href]",
    ]:
        for node in soup.select(selector):
            href = node.get("href", "")
            text = _clean_spaces(_text(node))
            url = _absolute_url(page_url, href)
            if _is_catalog_url(url) or any(label in text for label in CATALOG_LABELS):
                return url
    book_id = _book_id_from_url(page_url) or _book_id_from_soup(soup)
    return _catalog_url_from_book_id(book_id) if book_id else ""


def _local_companion_catalog(page_url: str) -> Optional[str]:
    if not _looks_like_local_file(page_url):
        return None
    source = Path(page_url)
    if not source.exists() or not source.parent.exists():
        return None
    for candidate in sorted(source.parent.glob("*.html")):
        if candidate.resolve() == source.resolve():
            continue
        try:
            soup = BeautifulSoup(candidate.read_text(encoding="utf-8", errors="replace"), "html.parser")
        except Exception:
            continue
        title = _text(soup.find("title"))
        if soup.select_one(".chaplist ul.all a[href]") or any(label in title for label in CHAPTER_LIST_LABELS):
            return str(candidate)
    return None


def _get_book_info(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, str]:
    title = (
        _meta_content(soup, "og:novel:book_name", "og:title")
        or _text(soup.select_one("#detail h1.title"))
        or _text(soup.select_one("section.info h1.title"))
        or _text(soup.select_one("h1.title"))
        or _text(soup.find("h1"))
    )
    if not title:
        title = _text(soup.find("title"))
    title = _clean_book_title(title) or "Unknown"

    author = _meta_content(soup, "og:novel:author") or _text(soup.select_one("#detail .author"))
    if not author:
        h2_text = _text(soup.select_one("section.info h2.title"))
        match = re.search(rf"{AUTHOR_LABEL}\s*[:\uff1a/]\s*([^|]+)", h2_text)
        if match:
            author = _clean_spaces(match.group(1))

    category = _meta_content(soup, "og:novel:category") or _field_from_spans(soup, *CATEGORY_LABELS)
    if not category:
        links = soup.select(".breadcrumb a")
        if len(links) >= 2:
            category = _text(links[1])

    status = _meta_content(soup, "og:novel:status")
    update_time = _meta_content(soup, "og:novel:update_time") or _field_from_spans(soup, UPDATE_LABEL)
    update_time = re.sub(r"[^0-9:\- ]+$", "", update_time).strip()

    latest_node = _find_latest_node(soup, page_url)
    latest_chapter = _text(latest_node) if latest_node else _meta_content(
        soup, "og:novel:latest_chapter_name", "og:novel:lastest_chapter_name"
    )
    latest_url = ""
    if latest_node and latest_node.get("href"):
        latest_url = _absolute_url(page_url, latest_node.get("href", ""))
    if not latest_url:
        latest_url = _meta_content(soup, "og:novel:latest_chapter_url", "og:novel:lastest_chapter_url")
        if latest_url:
            latest_url = _absolute_url(page_url, latest_url)

    cover_url = _meta_content(soup, "og:image")
    if not cover_url:
        img = soup.select_one("#detail .cover img[src], #detail img[src], img[src]")
        if img:
            cover_url = (img.get("data-src") or img.get("data-original") or img.get("src") or "").strip()
    if cover_url:
        cover_url = _absolute_url(page_url, cover_url)

    intro = _meta_content(soup, "og:description", "description")
    intro_node = soup.select_one("#detail .intro") or soup.select_one(".bookintro") or soup.select_one(".intro")
    if intro_node:
        intro = intro_node.get_text("\n", strip=True)
    intro = _clean_spaces(intro)

    book_id = _book_id_from_url(page_url) or _book_id_from_soup(soup)
    canonical = soup.select_one("link[rel='canonical'][href]")
    book_url = _meta_content(soup, "og:novel:read_url", "og:url")
    if not book_url and canonical:
        book_url = canonical.get("href", "")
    if book_id:
        book_url = _book_url_from_id(book_id)
    elif book_url:
        book_url = _absolute_url(page_url, book_url)

    catalog_url = _find_catalog_url(soup, page_url)

    total_chapters = 0
    for value in [_field_from_spans(soup, *CHAPTERS_LABELS), soup.get_text(" ", strip=True)[:2000]]:
        match = re.search(r"(?:\u7ae0\u7bc0|\u7ae0\u8282)\s*[:\uff1a]?\s*(\d+)", value)
        if match:
            total_chapters = int(match.group(1))
            break

    return {
        "title": title,
        "author": _clean_spaces(author) or "Unknown",
        "status": _clean_spaces(status),
        "category": _clean_spaces(category),
        "update_time": _clean_spaces(update_time),
        "latest_chapter": _clean_spaces(latest_chapter),
        "latest_chapter_url": latest_url or "",
        "cover_url": cover_url or "",
        "intro": intro or "",
        "total_chapters": total_chapters,
        "catalog_url": catalog_url,
        "url": book_url or page_url,
    }


def _extract_chapters_from_catalog(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    nodes = soup.select(".chaplist ul.all a[href], .chaplist .all a[href]")
    if not nodes:
        nodes = soup.select(".chaplist a[href]")

    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    for a in nodes:
        title = _clean_spaces(a.get("title") or _text(a))
        url = _absolute_url(page_url, a.get("href", ""))
        if not title or not _is_chapter_url(page_url, url, allow_part=True):
            continue
        key = _base_chapter_url(url)
        if key in seen:
            continue
        seen.add(key)
        chapters.append({"title": title, "url": _first_part_url(url)})

    chapters.sort(key=lambda item: (_chapter_number_from_url(item["url"]) or 10**9, item["url"]))
    return chapters


def _find_catalog_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> str:
    for selector in [".foot-nav a[href]", "a[href$='/dir']", "a[href*='/dir']"]:
        for a in soup.select(selector):
            text = _clean_spaces(_text(a))
            url = _absolute_url(chapter_url, a.get("href", ""))
            if _is_catalog_url(url) or any(label in text for label in CATALOG_LABELS):
                return url
    book_id = _book_id_from_url(chapter_url) or _book_id_from_soup(soup)
    return _catalog_url_from_book_id(book_id) if book_id else ""


def getText(url: str = DEFAULT_URL) -> Dict:
    url = _ensure_url(url)
    original_url = url
    soup = _fetch_html(url)

    if _is_chapter_url(url, url, allow_part=True):
        catalog_url = _find_catalog_url_from_chapter(soup, url)
        if catalog_url:
            url = catalog_url
            soup = _fetch_html(url, referer=original_url)

    info = _get_book_info(soup, url)
    catalog_url = info.get("catalog_url") or _find_catalog_url(soup, url)

    local_catalog = _local_companion_catalog(url)
    if local_catalog:
        catalog_url = local_catalog

    if _is_catalog_url(url) or soup.select_one(".chaplist ul.all a[href]"):
        catalog_soup = soup
        catalog_url = url
    elif catalog_url:
        catalog_soup = _fetch_html(catalog_url, referer=url)
    else:
        catalog_soup = soup

    if catalog_soup is not soup:
        catalog_info = _get_book_info(catalog_soup, catalog_url)
        for key in ("title", "author", "category", "update_time", "catalog_url", "url"):
            if not info.get(key) or info.get(key) == "Unknown":
                info[key] = catalog_info.get(key, info.get(key, ""))
        if not info.get("total_chapters"):
            info["total_chapters"] = catalog_info.get("total_chapters", 0)

    chapters = _extract_chapters_from_catalog(catalog_soup, catalog_url or url)
    if not chapters:
        chapters = _extract_chapters_from_catalog(soup, url)
    total = info.get("total_chapters") or len(chapters)

    return {
        "title": info.get("title", "Unknown"),
        "author": info.get("author", "Unknown"),
        "status": info.get("status", ""),
        "category": info.get("category", ""),
        "update_time": info.get("update_time", ""),
        "latest_chapter": info.get("latest_chapter", ""),
        "latest_chapter_url": info.get("latest_chapter_url", ""),
        "chapters": chapters,
        "total_chapters": total,
        "cover_url": info.get("cover_url", ""),
        "intro": info.get("intro", ""),
        "catalog_url": catalog_url or info.get("catalog_url", ""),
        "url": info.get("url", url),
    }


def _chapter_title_from_page(soup: BeautifulSoup, book_title: str = "", fallback: str = "") -> str:
    title = _text(soup.select_one(".chapter-content h1") or soup.find("h1"))
    if not title:
        title = _text(soup.find("title"))
    title = _clean_spaces(title)
    title = re.sub(r"\s*[\uff08(]\s*\d+\s*/\s*\d+\s*[\uff09)]\s*$", "", title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title) :].strip(" -_:\uff1a")
    for marker in (SITE_NAME, "novel543"):
        title = title.split(marker, 1)[0].strip(" -_|")
    return title or fallback or "Chapter"


def _chapter_page_count(soup: BeautifulSoup) -> int:
    text = _text(soup.select_one(".chapter-content h1") or soup.find("h1") or soup.find("title"))
    match = re.search(r"[\uff08(]\s*\d+\s*/\s*(\d+)\s*[\uff09)]", text)
    if match:
        try:
            return max(1, int(match.group(1)))
        except ValueError:
            return 1
    return 1


def _chapter_part_urls(url: str, total_pages: int) -> List[str]:
    first_url = _first_part_url(url)
    if total_pages <= 1:
        return [first_url]
    parsed = urlparse(first_url)
    match = re.search(r"^(?P<prefix>.*/\d+_\d+)\.html?$", parsed.path, flags=re.I)
    if not match:
        return [first_url]
    urls = []
    for page_no in range(1, total_pages + 1):
        path = f"{match.group('prefix')}.html" if page_no == 1 else f"{match.group('prefix')}_{page_no}.html"
        urls.append(parsed._replace(path=path, query="", fragment="").geturl())
    return urls


def _same_chapter_url(a: str, b: str) -> bool:
    a_id = _chapter_identity_from_url(a)
    b_id = _chapter_identity_from_url(b)
    return bool(a_id and b_id and a_id == b_id)


def _next_part_url(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False) or ""
        match = re.search(r"\bnextUrl\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match:
            next_url = _absolute_url(current_url, match.group(1))
            if next_url != current_url and _same_chapter_url(current_url, next_url):
                return next_url

    for a in soup.select(".foot-nav a[href]"):
        next_url = _absolute_url(current_url, a.get("href", ""))
        if next_url != current_url and _same_chapter_url(current_url, next_url):
            return next_url
    return None


TRASH_MARKERS = (
    "novel543.com",
    SITE_NAME,
    "TAMedia",
    "pubfuture",
    "clickforce",
    "Read.init",
    "window.",
    "document.",
    "function(",
    "adBlock",
    "google",
    "\u6eab\u99a8\u63d0\u793a",
    "\u6e29\u99a8\u63d0\u793a",
    "\u4e0a\u4e00\u7ae0",
    "\u4e0b\u4e00\u7ae0",
    "\u4e0a\u9801",
    "\u4e0b\u9801",
    "\u4e0a\u9875",
    "\u4e0b\u9875",
    "\u8a2d\u7f6e",
    "\u8bbe\u7f6e",
    "\u6536\u85cf",
    "\u5831\u932f",
    "\u62a5\u9519",
    "\u806f\u7d61\u6211\u5011",
    "\u8054\u7cfb\u6211\u4eec",
) + CATALOG_LABELS


def _strip_title_prefix(line: str, title: str = "") -> str:
    title = _clean_spaces(title)
    candidates = [title]
    candidates.append(re.sub(r"\s*[\uff08(]\s*\d+\s*/\s*\d+\s*[\uff09)]\s*$", "", title))
    for candidate in sorted({c for c in candidates if c}, key=len, reverse=True):
        if line.startswith(candidate):
            return line[len(candidate) :].strip()
    return line


def _clean_chapter_lines(raw_lines: List[str], title: str = "") -> List[str]:
    paragraphs: List[str] = []
    for raw_line in raw_lines:
        line = html.unescape(raw_line or "")
        line = line.replace("\xa0", " ").replace("\u3000", " ")
        line = _clean_spaces(line)
        if not line:
            continue
        line = _strip_title_prefix(line, title=title)
        line = _clean_spaces(line)
        if not line:
            continue
        if re.fullmatch(r"[\uff08(]?\s*\d+\s*/\s*\d+\s*[\uff09)]?", line):
            continue
        if any(marker and marker in line for marker in TRASH_MARKERS):
            continue
        if re.fullmatch(r"\(?https?://[^\s)]+\)?", line, flags=re.I):
            continue
        if len(line) < 2:
            continue
        paragraphs.append(line)
    return paragraphs


def _extract_chapter_paragraphs(soup: BeautifulSoup, title: str = "") -> List[str]:
    content = soup.select_one(".chapter-content .content") or soup.select_one(".content") or soup.select_one("article")
    if not content:
        candidates = [
            node
            for node in soup.select(".chapter-content, .reader, article, main")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 120
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return []

    content = BeautifulSoup(str(content), "html.parser")
    for node in content.find_all(["script", "style", "ins", "iframe", "select", "input", "button"]):
        node.decompose()
    for node in content.select(
        ".adBlock, .gadBlock, .float-wrap, .modal, .setting-body, .foot-nav, .nav, .opts, "
        "[data-ad], [id^='div-tam-ad'], [id^='pf-'], .clickforceads"
    ):
        node.decompose()
    for node in list(content.find_all(["div", "p"])):
        text = _clean_spaces(node.get_text(" ", strip=True))
        if len(text) < 300 and any(marker in text for marker in ("\u6eab\u99a8\u63d0\u793a", "\u6e29\u99a8\u63d0\u793a")):
            node.decompose()
    for a in content.find_all("a"):
        a.decompose()

    p_nodes = content.find_all("p")
    if p_nodes:
        return _clean_chapter_lines([p.get_text(" ", strip=True) for p in p_nodes], title=title)

    for br in content.find_all("br"):
        br.replace_with("\n")
    return _clean_chapter_lines(content.get_text("\n", strip=False).split("\n"), title=title)


def _chapter_content_html_from_pages(pages: List[BeautifulSoup], title: str = "") -> str:
    paragraphs: List[str] = []
    for page in pages:
        paragraphs.extend(_extract_chapter_paragraphs(page, title=title))
    if not paragraphs:
        return "<p>(Khong co noi dung)</p>"
    return "\n".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)


def _retry_delay_seconds(status_code: Optional[int], attempt: int) -> float:
    if status_code == 403:
        return min(20.0, 4.0 + attempt * 3.0)
    if status_code == 429:
        return min(25.0, 5.0 * attempt)
    return max(SLEEP_BETWEEN_CHAPS, 1.5 * attempt)


def fetch_chapter_content(
    url: str,
    retries: int = CHAPTER_RETRIES,
    *,
    fallback_title: str = "",
    book_title: str = "",
) -> Dict:
    url = _ensure_url(url)
    local_input = _looks_like_local_file(url)
    first_url = url if local_input else _first_part_url(url)
    last_status: Optional[int] = None

    for attempt in range(1, retries + 1):
        try:
            first_soup, status_code = _fetch_html_with_status(first_url, tries=1, referer=_chapter_referer(first_url))
            last_status = status_code if isinstance(status_code, int) else None
            title = _chapter_title_from_page(first_soup, book_title=book_title, fallback=fallback_title)

            pages = [first_soup]
            visited = {_normalized_url(first_url)}
            page_urls = _chapter_part_urls(first_url, _chapter_page_count(first_soup))
            for page_url in page_urls[1:]:
                if local_input and not _looks_like_local_file(page_url):
                    continue
                key = _normalized_url(page_url)
                if key in visited:
                    continue
                visited.add(key)
                time.sleep(SLEEP_BETWEEN_PAGES)
                pages.append(_fetch_html(page_url, referer=first_url))

            current_soup = pages[-1]
            current_url = page_urls[min(len(pages), len(page_urls)) - 1] if page_urls else first_url
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
                "url": first_url,
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
        "url": first_url,
        "status_code": last_status or "ERR",
    }


def _chapter_html_doc(title: str, content_html: str, source_url: str = "") -> str:
    source = f'<p class="source"><a href="{html.escape(source_url)}">{html.escape(source_url)}</a></p>' if source_url else ""
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
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


def _flat_chapter_html_path(book_dir: str | Path, idx: int, title: str = "") -> Path:
    Path(book_dir).mkdir(parents=True, exist_ok=True)
    suffix = f" - {_safe_filename(title, 100)}" if title else ""
    return Path(book_dir) / f"{idx:04d}{suffix}.html"


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


def save_all_chapters_to_html(
    book_title: str,
    chapters: List[Dict[str, str]],
    out_dir: str | Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
    force: bool = False,
) -> List[Dict[str, str]]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = _normalize_range(len(chapters), start, end)
    selected_total = end - start + 1
    saved: List[Dict[str, str]] = []

    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        existing = sorted(out_dir.glob(f"{idx:04d}*.html"))
        if existing and not force:
            data = _read_cached_chapter(existing[0])
        else:
            data = fetch_chapter_content(
                chapter["url"],
                fallback_title=chapter.get("title", f"Chapter {idx}"),
                book_title=book_title,
            )
            if not _is_failed_chapter_data(data):
                html_path = _flat_chapter_html_path(out_dir, idx, data.get("title") or chapter.get("title") or f"Chapter {idx}")
                html_path.write_text(
                    _chapter_html_doc(data.get("title") or f"Chapter {idx}", data.get("content_html") or "", data.get("url") or chapter.get("url", "")),
                    encoding="utf-8",
                )
                data["html_path"] = str(html_path)
        saved.append(data)
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
    return saved


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
            language="zh-Hant",
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
        response = _http_get(cover_url, referer=BASE_URL)
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
    _safe_print("Downloader novel543.com / Ji Xia Shu Yuan")
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
    examples = """examples:
  python novel543.py
  python novel543.py https://www.novel543.com/0327692090/ -y
  python novel543.py https://www.novel543.com/0327692090/ --manual-verify --manual-timeout 300 -y
  python novel543.py https://www.novel543.com/0327692090/ --start 17 --end 17 --no-epub -y
  python novel543.py https://www.novel543.com/0327692090/ --cookie-file novel543_cookie.txt -y
  python novel543.py https://www.novel543.com/0327692090/ --manual-verify --manual-keep-open -y
"""
    parser = argparse.ArgumentParser(
        description="Download novel543.com novel chapters and build EPUB.",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", nargs="?", help=f"Book/catalog/chapter URL. Default: {DEFAULT_URL}")
    parser.add_argument("--start", type=int, default=1, help="Start chapter index")
    parser.add_argument("--end", type=int, default=None, help="End chapter index")
    parser.add_argument("--force", action="store_true", help="Refetch even when cache exists")
    parser.add_argument("--no-epub", action="store_true", help="Only download/cache HTML")
    parser.add_argument("--cookie", default="", help="Raw Novel543 Cookie header copied from a verified browser")
    parser.add_argument("--cookie-file", default="", help="File containing a raw Cookie header or Netscape cookies.txt")
    parser.add_argument("--user-agent", default="", help="User-Agent copied from the same verified browser session")
    parser.add_argument("--cloak", action="store_true", help="Use CloakBrowser fallback instead of normal Playwright Chromium")
    parser.add_argument("--cloak-profile", default="", help="Persistent CloakBrowser profile directory")
    parser.add_argument("--cloak-disable-http2", action="store_true", help="Pass --disable-http2 to CloakBrowser")
    parser.add_argument("--manual-verify", action="store_true", help="Open real Chrome/Edge, wait for manual verification, then continue with cookies")
    parser.add_argument("--manual-profile", default="", help="Profile directory for the real Chrome/Edge verification browser")
    parser.add_argument("--manual-port", type=int, default=None, help="DevTools port for manual verification browser")
    parser.add_argument("--manual-timeout", type=int, default=None, help="Seconds to wait for manual verification")
    parser.add_argument("--manual-browser", default="", help="Path to real Chrome/Edge used for manual verification")
    parser.add_argument("--manual-keep-open", action="store_true", help="Keep the manual verification browser open after cookies are captured")
    parser.add_argument("-y", "--yes", action="store_true", help="Run non-interactively")
    args = parser.parse_args(argv)
    interactive_mode = not args.yes and not args.url
    _configure_user_agent(args.user_agent)
    _configure_cookie(args.cookie, args.cookie_file or None)
    _configure_cloak_browser(args.cloak, args.cloak_profile, args.cloak_disable_http2)
    _configure_manual_verify(
        args.manual_verify or interactive_mode,
        args.manual_profile,
        args.manual_port,
        args.manual_timeout,
        args.manual_browser,
        args.manual_keep_open,
    )

    if not args.manual_verify and interactive_mode:
        _safe_print("Manual verify se tu bat khi Novel543 yeu cau xac minh.")

    if args.yes or args.url:
        _run_once(args)
    else:
        _interactive_main()


if __name__ == "__main__":
    main()
