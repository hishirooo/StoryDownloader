# -*- coding: utf-8 -*-
"""
novembrehiver_wp.py
---------------------------------
Simple downloader for novembrehiver.wordpress.com story pages.
- Tải danh sách chương từ trang truyện / trang chương
- Tự động lấy cover và resize bằng Pillow nếu có
- Lưu HTML chương và xuất EPUB bằng epub_builder
"""

from __future__ import annotations

import html as html_module
import io
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:  # pragma: no cover
    cloudscraper = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

from epub_builder import create_epub

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}
DEFAULT_OUTPUT_DIR = Path("output")
MAX_COVER_SIZE = (1600, 2400)


def _safe_filename(value: str, max_length: int = 140) -> str:
    if not value:
        return "novembrehiver"
    value = str(value).strip()
    value = html_module.unescape(value)
    value = re.sub(r"[\\/:*?\"<>|]+", " - ", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    return value[:max_length] or "novembrehiver"


def _slugify(value: str) -> str:
    value = _safe_filename(value)
    value = value.lower()
    value = re.sub(r"[^0-9a-z\- ]+", "", value)
    value = re.sub(r"[\s]+", "-", value)
    return value or "novembrehiver"


class _PlaywrightResponse:
    def __init__(self, status_code: int, headers: Dict[str, str], text: str, content: bytes, url: str) -> None:
        self.status_code = status_code
        self.headers = headers
        self._text = text
        self.content = content
        self.url = url
        self.encoding = "utf-8"

    @property
    def text(self) -> str:
        return self._text

    def raise_for_status(self) -> None:
        if 400 <= self.status_code:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)


class _PlaywrightSession:
    def __init__(self, timeout: int = 30) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._timeout = timeout

    def _ensure_browser(self) -> None:
        if self._pw is not None:
            return
        if sync_playwright is None:
            raise RuntimeError("Playwright is not installed")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )

    def get(self, url: str, headers: Optional[Dict[str, str]] = None, timeout: Optional[int] = None):
        self._ensure_browser()
        page = self._context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page_headers = dict(headers or {})
        referer = page_headers.pop("Referer", None)
        if page_headers:
            page.set_extra_http_headers(page_headers)

        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=(timeout or self._timeout) * 1000,
            referer=referer,
        )
        page.wait_for_timeout(2000)

        if response is None:
            page.close()
            raise requests.RequestException(f"Không thể tải {url}")

        if "Checking your browser" in page.title() or "Just a moment" in page.title():
            try:
                page.wait_for_selector("article, .entry-title, .entry-content", timeout=20000)
            except Exception:
                pass

        html = page.content()
        is_challenge = any(token in page.title() for token in ["Checking your browser", "Just a moment", "Attention Required"])
        status_code = response.status
        if status_code == 403 and not is_challenge and ("<article" in html or "entry-title" in html or "entry-content" in html):
            status_code = 200

        body = html.encode("utf-8")
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        page.close()

        return _PlaywrightResponse(
            status_code=response.status,
            headers=headers_lower,
            text=html,
            content=body,
            url=url,
        )

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass


def _http_session() -> requests.Session:
    if sync_playwright is not None:
        return _PlaywrightSession()

    if cloudscraper is not None:
        sess = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    else:
        sess = requests.Session()
        adapter = requests.adapters.HTTPAdapter(max_retries=3)
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
    sess.headers.update(HEADERS)
    try:
        sess.get("https://novembrehiver.wordpress.com/", timeout=25)
    except Exception:
        pass
    return sess


def _http_get(url: str, sess: requests.Session, referer: Optional[str] = None, retries: int = 3, backoff: float = 0.8) -> Optional[requests.Response]:
    headers = {}
    headers["Referer"] = referer or "https://novembrehiver.wordpress.com/"

    for attempt in range(1, retries + 1):
        try:
            response = sess.get(url, headers=headers, timeout=25)
            if response.status_code == 403 and attempt < retries:
                time.sleep(backoff * attempt)
                if not headers.get("Referer"):
                    headers["Referer"] = "https://novembrehiver.wordpress.com/"
                continue
            if response.status_code == 403 and attempt == retries:
                fresh = requests.Session()
                fresh.headers.update(HEADERS)
                if not headers.get("Referer"):
                    headers["Referer"] = "https://novembrehiver.wordpress.com/"
                fresh.get("https://novembrehiver.wordpress.com/", timeout=25)
                fresh_response = fresh.get(url, headers=headers, timeout=25)
                if fresh_response.status_code == 200:
                    return fresh_response
            return response
        except requests.RequestException:
            if attempt < retries:
                time.sleep(backoff * attempt)
                continue
            return None
    return None


def _fetch_html(url: str, sess: requests.Session, referer: Optional[str] = None) -> BeautifulSoup:
    response = _http_get(url, sess, referer=referer)
    if response is None:
        raise requests.RequestException(f"Không thể lấy {url}")
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding
    return BeautifulSoup(response.text, "html.parser")


def _escape_url(url: str) -> str:
    return url.strip()


def _normalize_url(base_url: str, href: str) -> str:
    return urljoin(base_url, href.strip())


def _cover_extension_from_url(url: str, content_type: str = "") -> str:
    path = urlparse(url).path
    ext = Path(path).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if ext == ".jpeg" else ext
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    return ".jpg"


def _download_bytes(url: str, sess: requests.Session, referer: Optional[str] = None) -> bytes:
    response = _http_get(url, sess, referer=referer)
    if response is None:
        raise requests.RequestException(f"Không thể tải bytes từ {url}")
    response.raise_for_status()
    return response.content


def _resize_cover(content: bytes, ext: str) -> Tuple[bytes, str]:
    if not content or not HAS_PILLOW:
        return content, ext
    try:
        image = Image.open(io.BytesIO(content)).convert("RGB")
        image.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88)
        return output.getvalue(), ".jpg"
    except Exception:
        return content, ext


def _extract_story_info(url: str, sess: requests.Session) -> Tuple[str, str, List[Dict[str, str]]]:
    soup = _fetch_html(url, sess, referer="https://novembrehiver.wordpress.com/")

    title = None
    if soup.select_one('meta[property="og:title"]'):
        title = soup.select_one('meta[property="og:title"]').get("content", "")
    if not title and soup.select_one("h1.entry-title"):
        title = soup.select_one("h1.entry-title").get_text(strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = title or "Truyện NovembreHiver"

    cover_url = ""
    if soup.select_one('meta[property="og:image"]'):
        cover_url = soup.select_one('meta[property="og:image"]').get("content", "")

    if not cover_url:
        img = soup.select_one(".entry-content img") or soup.select_one("article img")
        if img and img.get("src"):
            cover_url = _normalize_url(url, img["src"])

    chapters = _find_chapter_links(soup, url)
    if not chapters:
        chapters = [{"title": title, "url": url}]

    return title, cover_url, chapters


def _is_chapter_link(text: str, href: str) -> bool:
    if not href or not text:
        return False
    text_lower = text.lower()
    href_lower = href.lower()
    if "trang-sang-ngan-van-dam-chuong" in href_lower:
        return True
    if "chuong" in text_lower and href_lower.startswith("http"):
        return True
    if re.match(r"^\d{1,4}$", text):
        return True
    return False


def _find_chapter_links(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    container = soup.select_one(".entry-content") or soup.body or soup
    if not container:
        return []

    items: List[Dict[str, str]] = []
    seen: set[str] = set()

    for a in container.find_all("a", href=True):
        href = _normalize_url(base_url, a["href"])
        text = a.get_text(" ", strip=True)
        if not _is_chapter_link(text, href):
            continue
        if href in seen:
            continue
        seen.add(href)
        items.append({"title": text or href, "url": href})

    def sort_key(item: Dict[str, str]) -> Tuple[int, str]:
        text = item["title"]
        m = re.search(r"(\d+)", text)
        if m:
            return int(m.group(1)), text
        m2 = re.search(r"chuong-(\d+)", item["url"].lower())
        if m2:
            return int(m2.group(1)), text
        return (9999, text)

    items.sort(key=sort_key)
    return items


def _clean_chapter_html(soup: BeautifulSoup) -> str:
    content = soup.select_one(".entry-content") or soup.select_one("article") or soup.body or soup
    if not content:
        return "<p>(Không tìm thấy nội dung chương.)</p>"

    for bad in content.select(
        "script, style, iframe, noscript, .sharedaddy, .sd-sharing-enabled, .sd-like-enabled, "
        ".jetpack-sharing-buttons, .jetpack-sharing-button, .wp-block-jetpack-sharing-buttons, "
        ".comments-area, .wp-block-comments, .post-navigation, .related-posts, .wp-block-social-links"
    ):
        bad.decompose()

    for a in content.find_all("a"):
        if a.get("href") and a.get_text(strip=True) == a.get("href"):
            a.unwrap()

    return str(content)


def _extract_chapter_data(url: str, sess: requests.Session) -> Dict[str, Any]:
    soup = _fetch_html(url, sess, referer="https://novembrehiver.wordpress.com/")
    title = None
    if soup.select_one('meta[property="og:title"]'):
        title = soup.select_one('meta[property="og:title"]').get("content", "")
    if not title and soup.select_one("h1.entry-title"):
        title = soup.select_one("h1.entry-title").get_text(strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = title or "Chương"

    return {
        "title": title,
        "content_html": _clean_chapter_html(soup),
        "url": url,
    }


def _download_cover(cover_url: str, sess: requests.Session, referer: Optional[str] = None) -> Tuple[Optional[bytes], Optional[str]]:
    if not cover_url:
        return None, None
    try:
        resp = _http_get(cover_url, sess, referer=referer)
        if resp is None:
            return None, None
        resp.raise_for_status()
        ext = _cover_extension_from_url(cover_url, resp.headers.get("content-type", ""))
        return resp.content, ext
    except Exception:
        return None, None


def _write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _save_chapter_html(chapter: Dict[str, str], book_dir: Path, idx: int) -> Path:
    title = chapter.get("title") or f"Chương {idx}"
    fname = f"{idx:04d} - {_safe_filename(title)}.html"
    path = book_dir / "html" / fname
    _write_text_file(path, f"<!doctype html>\n<html><head><meta charset=\"utf-8\"/><title>{html_module.escape(title)}</title></head><body>{chapter.get('content_html') or ''}</body></html>")
    return path


def _save_cover_file(content: bytes, ext: str, book_dir: Path) -> Optional[Path]:
    if not content or not ext:
        return None
    cover_path = book_dir / f"cover{ext}"
    cover_path.write_bytes(content)
    return cover_path


def main() -> None:
    if len(sys.argv) > 1:
        url = sys.argv[1].strip()
    else:
        url = input("URL truyện hoặc chương novembrehiver: ").strip()

    if not url:
        print("Không có URL. Thoát.")
        return

    output_dir = DEFAULT_OUTPUT_DIR
    if len(sys.argv) > 2:
        output_dir = Path(sys.argv[2].strip()) or output_dir

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    sess = _http_session()
    story_title, cover_url, chapters = _extract_story_info(url, sess)
    book_slug = _slugify(story_title)
    book_dir = output_dir / book_slug
    book_dir.mkdir(parents=True, exist_ok=True)

    print(f"Story: {story_title}")
    print(f"Cover: {cover_url}")
    print(f"Chapters: {len(chapters)}")

    cover_bytes, cover_ext = _download_cover(cover_url, sess)
    if cover_bytes and cover_ext:
        if HAS_PILLOW:
            try:
                import io
                cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
            except Exception:
                pass
        saved_cover = _save_cover_file(cover_bytes, cover_ext, book_dir)
        if saved_cover:
            print(f"Đã lưu cover: {saved_cover}")

    chapters_data: List[Dict[str, Any]] = []
    for idx, chapter in enumerate(chapters, 1):
        print(f"Tải chương {idx}/{len(chapters)}: {chapter['title']}")
        data = _extract_chapter_data(chapter["url"], sess)
        chapters_data.append(data)
        _save_chapter_html(data, book_dir, idx)
        time.sleep(0.15)

    epub_path = book_dir / f"{_safe_filename(story_title)}.epub"
    create_epub(
        book_url=url,
        book_title=story_title,
        author="novembrehiver",
        chapters=[{"title": item["title"], "url": item["url"]} for item in chapters],
        chapters_data=chapters_data,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext or ".jpg",
        out_epub_path=str(epub_path),
        language="vi",
        creator="StoryDownloader",
        publisher="novembrehiver",
        tags=["WordsPress", "novembrehiver"],
    )
    print(f"Hoàn thành EPUB: {epub_path}")

    if hasattr(sess, "close"):
        try:
            sess.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
