# -*- coding: utf-8 -*-
"""
Downloader for https://truyencom.com/.

Book sample:
  https://truyencom.com/con-duong-ba-chu.66/

Chapter sample:
  https://truyencom.com/con-duong-ba-chu/chuong-96.html
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
import json
import math
import re
import sys
import time
import unicodedata

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

BASE_URL = "https://truyencom.com/"
DEFAULT_URL = "https://truyencom.com/con-duong-ba-chu.66/"
OUTPUT_BASE = Path("output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8,zh;q=0.7",
    "Content-Language": "vi",
    "Referer": BASE_URL,
}

API_LIMIT = 50
TIMEOUT = 25
SLEEP_BETWEEN_PAGES = 0.25
SLEEP_BETWEEN_CHAPS = 0.25
CHAPTER_RETRIES = 4
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
    value = value.replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def _safe_filename(name: str, max_length: int = 150) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', " - ", name or "book")
    safe = re.sub(r"\s+", " ", safe).strip().strip(".")
    return (safe[:max_length].strip() or "book")


def slugify_vi(value: str) -> str:
    return _safe_filename(value)


def _strip_accents(value: str) -> str:
    value = unicodedata.normalize("NFD", value or "")
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return value.replace("đ", "d").replace("Đ", "D")


def _slugify_url(value: str) -> str:
    value = _strip_accents(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-")


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
    if not href:
        return page_url
    if href.startswith("//"):
        return "https:" + href
    base = page_url if urlparse(page_url).scheme else BASE_URL
    return urljoin(base, href)


def _http_referer(value: str) -> str:
    return value if urlparse(value or "").scheme in {"http", "https"} else BASE_URL

def _normalized_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return str(Path(url))
    return parsed._replace(fragment="", query="").geturl().rstrip("/")


def _http_get(url: str, *, referer: Optional[str] = None, accept_json: bool = False):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    if accept_json:
        headers["Accept"] = "*/*"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Site"] = "same-origin"
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
    candidates.extend(["utf-8", "cp1258", "windows-1258"])
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


def _script_text(soup: BeautifulSoup) -> str:
    return "\n".join(script.get_text("\n", strip=False) for script in soup.find_all("script"))


def _story_id_from_url(url: str) -> str:
    match = re.search(r"\.(\d+)/?$", urlparse(url).path.rstrip("/"))
    return match.group(1) if match else ""


def _story_alias_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if "/" in path:
        path = path.split("/", 1)[0]
    path = re.sub(r"\.\d+$", "", path)
    return path


def _book_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> str:
    for a in soup.select(".breadcrumb a[href], .chapter a.truyen-title[href], a.truyen-title[href]"):
        href = a.get("href", "")
        if re.search(r"\.\d+/?$", urlparse(href).path):
            return _absolute_url(chapter_url, href)
    script = _script_text(soup)
    match = re.search(r"story\s*=\s*\{.*?storyID\s*:\s*(\d+).*?alias\s*:\s*['\"]([^'\"]+)", script, flags=re.S)
    if match:
        return f"{BASE_URL.rstrip('/')}/{match.group(2)}.{match.group(1)}/"
    return DEFAULT_URL


def _extract_book_info(soup: BeautifulSoup, page_url: str) -> Dict[str, str]:
    canonical = soup.select_one("link[rel='canonical'][href]")
    book_url = _absolute_url(page_url, canonical["href"]) if canonical and canonical.get("href") else page_url
    script = _script_text(soup)

    title = (
        _text(soup.select_one("h3.title, h1[itemprop='name'], h1"))
        or re.sub(r"\s+-\s+.*$", "", _text(soup.find("title")))
    )
    author = _text(soup.select_one("a[itemprop='author']"))
    if not author:
        match = re.search(r"tác giả\s+(.+?)\s+số chương", _meta_content(soup, "description"), flags=re.I)
        author = match.group(1).strip() if match else ""

    info_node = soup.select_one(".info-holder .info, .col-info-desc .info, #truyen .info, .info")

    genres = []
    genre_scope = info_node if info_node else soup
    for a in genre_scope.select("a[itemprop='genre']"):
        text = _text(a)
        if text and text not in genres:
            genres.append(text)

    status = ""
    if info_node:
        text = _clean_spaces(info_node.get_text(" ", strip=True))
        match = re.search(r"Trạng thái\s*:\s*(.*?)(?:\s+TAGS\s*:|\s+Đánh giá\s*:|$)", text, flags=re.I)
        if match:
            status = match.group(1).strip()
    if not status:
        match = re.search(r"tình trạng\s+([^\.]+)", _meta_content(soup, "description"), flags=re.I)
        status = match.group(1).strip() if match else ""

    intro_node = soup.select_one(".desc-text, .desc, [itemprop='description']")
    intro = _clean_spaces(intro_node.get_text("\n", strip=True) if intro_node else _meta_content(soup, "description"))

    cover_url = ""
    img = soup.select_one(".book img, .info-holder img, img[itemprop='image'], img[src*='_cover']")
    if img:
        cover_url = (img.get("data-src") or img.get("src") or "").strip()
        if _looks_like_local_file(page_url) and cover_url.startswith("./"):
            cover_url = str((Path(page_url).parent / cover_url).resolve())
        else:
            cover_url = _absolute_url(page_url, cover_url)

    story_id = _story_id_from_url(book_url)
    story_alias = _story_alias_from_url(book_url)
    match = re.search(r"storyID\s*=\s*(\d+)", script)
    if match:
        story_id = match.group(1)
    match = re.search(r"storyAlias\s*=\s*['\"]([^'\"]+)", script)
    if match:
        story_alias = match.group(1)

    total_chapters = 0
    desc = _meta_content(soup, "description")
    match = re.search(r"số chương\s+(\d+)", desc, flags=re.I)
    if match:
        total_chapters = int(match.group(1))
    else:
        last = 0
        for a in soup.select("#list-chapter .list-chapter a[href]"):
            m = re.search(r"Chương\s+(\d+)", _text(a), flags=re.I)
            if m:
                last = max(last, int(m.group(1)))
        total_chapters = last

    latest_node = soup.select_one("#list-chapter .list-chapter a[href]")
    latest_chapter = _text(latest_node) if latest_node else ""
    latest_chapter_url = _absolute_url(book_url, latest_node.get("href", "")) if latest_node else ""

    return {
        "title": _clean_spaces(title) or "Truyen",
        "author": _clean_spaces(author) or "Unknown",
        "category": ", ".join(genres),
        "genre": ", ".join(genres),
        "status": _clean_spaces(status),
        "intro": intro,
        "cover_url": cover_url,
        "url": book_url,
        "story_id": story_id,
        "story_alias": story_alias,
        "total_chapters": total_chapters,
        "latest_chapter": latest_chapter,
        "latest_chapter_url": latest_chapter_url,
    }


def _chapter_title_from_item(item: Dict) -> str:
    for key in ("chapter_name", "name", "title", "chapterTitle"):
        value = item.get(key)
        if value:
            return _clean_spaces(str(value))
    number = item.get("chapter_no") or item.get("chapterNo") or item.get("number")
    return f"Chương {number}" if number else ""


def _chapter_url_from_item(item: Dict, book_info: Dict[str, str]) -> str:
    for key in ("url", "href", "link"):
        value = item.get(key)
        if value:
            return _absolute_url(book_info.get("url") or BASE_URL, str(value))
    alias = item.get("alias") or item.get("slug")
    title = _chapter_title_from_item(item)
    if not alias and title:
        alias = _slugify_url(title.split(":", 1)[0])
    return f"{BASE_URL.rstrip('/')}/{book_info.get('story_alias')}/{alias}.html" if alias else ""


def _parse_chapters_from_html(soup: BeautifulSoup, book_url: str) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.select("#list-chapter .list-chapter a[href], #list-chapter ul.list-chapter li a[href]"):
        title = _clean_spaces(a.get("title") or _text(a))
        title = re.sub(r"^.+?\s+-\s+(Chương\s+)", r"\1", title, flags=re.I)
        url = _absolute_url(book_url, a.get("href", ""))
        key = _normalized_url(url)
        if not title or key in seen:
            continue
        seen.add(key)
        chapters.append({"title": title, "url": url})
    return chapters


def _api_get_json(url: str, referer: str) -> Dict:
    response = _http_get(url, referer=referer, accept_json=True)
    response.raise_for_status()
    try:
        return response.json()
    except Exception:
        return json.loads(response.text)


def _extract_api_items(data) -> List[Dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("items", "chapters", "data", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_api_items(value)
            if nested:
                return nested
    return []


def _fetch_chapters_from_api(book_info: Dict[str, str], *, max_pages: Optional[int] = None) -> List[Dict[str, str]]:
    story_id = book_info.get("story_id") or _story_id_from_url(book_info.get("url", ""))
    if not story_id:
        return []
    total = int(book_info.get("total_chapters") or 0)
    max_pages = max_pages or (math.ceil(total / API_LIMIT) if total else 1)
    referer = book_info.get("url") or DEFAULT_URL
    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        api_url = f"{BASE_URL.rstrip('/')}/api/chapters/{story_id}/{page}/{API_LIMIT}"
        try:
            data = _api_get_json(api_url, referer)
        except Exception as exc:
            _safe_print(f"Canh bao: bo qua API muc luc page {page}: {exc}")
            break
        items = _extract_api_items(data)
        if not items:
            break
        for item in items:
            title = _chapter_title_from_item(item)
            url = _chapter_url_from_item(item, book_info)
            key = _normalized_url(url)
            if not title or not url or key in seen:
                continue
            seen.add(key)
            chapters.append({"title": title, "url": url})
        if len(items) < API_LIMIT:
            break
        time.sleep(SLEEP_BETWEEN_PAGES)
    return chapters


def getText(url: str = DEFAULT_URL) -> Dict:
    url = _ensure_url(url)
    soup = _fetch_html(url)
    local_input = _looks_like_local_file(url)

    if "/chuong-" in urlparse(url).path:
        book_url = _book_url_from_chapter(soup, url)
        if not local_input:
            soup = _fetch_html(book_url, referer=url)
            url = book_url

    info = _extract_book_info(soup, url)
    if local_input:
        chapters = _parse_chapters_from_html(soup, info["url"])
    else:
        chapters = _fetch_chapters_from_api(info)
        if not chapters:
            chapters = _parse_chapters_from_html(soup, info["url"])

    if chapters:
        info["latest_chapter"] = chapters[-1]["title"]
        info["latest_chapter_url"] = chapters[-1]["url"]

    return {
        **info,
        "chapters": chapters,
        "total_chapters": info.get("total_chapters") or len(chapters),
    }


def _chapter_title_from_page(soup: BeautifulSoup, fallback: str = "") -> str:
    title = _text(soup.select_one(".chapter-title")) or _text(soup.select_one("h2, h1"))
    if not title:
        title = re.sub(r"\s+của truyện.*$", "", _text(soup.find("title")), flags=re.I)
    return _clean_spaces(title) or fallback or "Chương"


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    trash = re.compile(
        r"(Truyencom\.com|Bạn đang đọc truyện|Báo lỗi chương|Chương trước|Chương tiếp|trở về đầu trang|"
        r"truyện đang hot|truyện cùng tác giả|website hoạt động|Creative Commons|Google|adsbygoogle)",
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
    content = soup.select_one(".chapter-c")
    if not content:
        candidates = [
            node for node in soup.select(".chapter, article, .content")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 300
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return []

    content = BeautifulSoup(str(content), "html.parser")
    for node in content.find_all(["script", "style", "ins", "iframe", "select", "input"]):
        node.decompose()
    for node in content.select(".ads, .chapter-nav, .highlight-box, .list-tags, .google-auto-placed"):
        node.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")
    return _clean_chapter_lines(content.get_text("\n", strip=False), title=title)


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
            soup, status_code = _fetch_html_with_status(url, tries=1, referer=BASE_URL)
            last_status = status_code if isinstance(status_code, int) else None
            title = _chapter_title_from_page(soup, fallback=fallback_title)
            paragraphs = _extract_chapter_paragraphs(soup, title=title)
            if not paragraphs:
                raise FetchHtmlError("No chapter content", last_status)
            content_html = "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
            text = "\n".join(paragraphs)
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
<html lang="vi">
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
    html_path = _chapter_html_path(book_dir, idx, data.get("title") or f"Chương {idx}")
    html_path.write_text(_chapter_html_doc(data.get("title") or f"Chương {idx}", data.get("content_html") or "", data.get("url") or source_url), encoding="utf-8")
    data["html_path"] = str(html_path)
    return html_path


def _save_chapter_html(chapter: Dict[str, str], idx: int, book_dir: str | Path, *, force: bool = False) -> Dict[str, str]:
    cached_path = _find_cached_chapter_path(book_dir, idx)
    if cached_path and not force:
        data = _read_cached_chapter(cached_path)
        if not _is_failed_chapter_data(data):
            return data
    data = fetch_chapter_content(chapter["url"], fallback_title=chapter.get("title", f"Chương {idx}"))
    if not _is_failed_chapter_data(data):
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
    book_dir = Path(book_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    start, end = _normalize_range(len(chapters), start, end)
    selected_total = end - start + 1
    downloaded: List[Dict[str, str]] = []
    _safe_print(f"Bat dau tai/cache {selected_total} chuong vao: {book_dir}")
    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        data = _save_chapter_html(chapter, idx, book_dir, force=force)
        downloaded.append(data)
        _safe_print(chapter_log_line(done, selected_total, data.get("status_code", "ERR"), idx, len(chapters), data.get("title") or chapter.get("title") or ""))
    _safe_print(f"Hoan tat tai/cache {selected_total} chuong.")
    return downloaded


def save_all_chapters_to_html(title: str, chapters: List[Dict[str, str]], out_dir: str, start: int = 1, end=None) -> None:
    book_info = {"title": title}
    download_chapters(book_info, chapters, out_dir, start=start, end=end)


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
    selected = chapters[start - 1 : end]
    chapters_data = _selected_chapter_data(book_info, chapters, book_dir, start=start, end=end)
    suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    epub_path = book_dir / f"{_safe_filename(book_info.get('title', 'Truyen'))}{suffix}.epub"
    noise = io.StringIO()
    with redirect_stdout(noise):
        epub_builder.create_epub(
            book_url=book_info.get("url", ""),
            book_title=book_info.get("title", "Truyen"),
            author=book_info.get("author", "Unknown"),
            chapters=selected,
            fetch_fn=fetch_chapter_content,
            cover_bytes=cover_bytes,
            cover_ext=cover_ext or ".jpg",
            out_epub_path=str(epub_path),
            html_cache_dir=None,
            chapters_data=chapters_data,
            language="vi",
            tags=book_info.get("category", ""),
            book_info=book_info,
        )
    _safe_print(f"[Epub] Da tao xong ebook: {epub_path}")
    return epub_path


def _download_cover(cover_url: str) -> Tuple[Optional[bytes], Optional[str]]:
    if not cover_url:
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
    if "/chuong-" in urlparse(book_page_url).path:
        book_url = _book_url_from_chapter(soup, book_page_url)
        soup = _fetch_html(book_url, referer=_http_referer(book_page_url))
        page_url = book_url
    info = _extract_book_info(soup, page_url)
    cover_url = info.get("cover_url") or ""
    if not cover_url:
        book_url = _book_url_from_chapter(soup, page_url)
        if book_url and _normalized_url(book_url) != _normalized_url(page_url):
            soup = _fetch_html(book_url, referer=_http_referer(page_url))
            info = _extract_book_info(soup, book_url)
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


def _load_book_context(url: str) -> Tuple[Dict[str, str], List[Dict[str, str]], Path, Optional[bytes], Optional[str]]:
    if not url:
        raise ValueError("Can nhap URL truyen hoac chuong.")
    _safe_print("Dang lay thong tin truyen...")
    data = getText(url)
    book_dir = _prepare_book_dir(data)
    chapters = data.get("chapters", [])

    _safe_print("\n-----------------Thong tin truyen-----------------")
    _safe_print(f"Ten truyen    : {data.get('title') or 'Unknown'}")
    _safe_print(f"Tac gia       : {data.get('author') or 'Unknown'}")
    if data.get("status"):
        _safe_print(f"Trang thai    : {data.get('status')}")
    if data.get("category"):
        _safe_print(f"The loai      : {data.get('category')}")
    _safe_print(f"So chuong     : {len(chapters)}")
    if data.get("latest_chapter"):
        _safe_print(f"Moi nhat      : {data.get('latest_chapter')}")
    _safe_print(f"Thu muc truyen: {book_dir}")
    _safe_print(f"EPUB se luu   : {book_dir / (_safe_filename(data.get('title') or 'Truyen') + '.epub')}")

    cover_bytes, cover_ext = _load_cover(data, book_dir)
    return data, chapters, book_dir, cover_bytes, cover_ext

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
    data, chapters, book_dir, cover_bytes, cover_ext = _load_book_context(args.url)
    if not chapters:
        raise RuntimeError("Khong tim thay chuong")
    start, end = _normalize_range(len(chapters), args.start, args.end)
    download_chapters(data, chapters, book_dir, start=start, end=end, force=args.force)
    if not args.no_epub:
        build_epub(data, chapters, book_dir, start=start, end=end, cover_bytes=cover_bytes, cover_ext=cover_ext)

def _interactive_main() -> None:
    _safe_print("Downloader truyencom.com")
    while True:
        raw_url = input("Nhap URL (bo trong de thoat): ").strip()
        if not raw_url:
            return
        try:
            data, chapters, book_dir, cover_bytes, cover_ext = _load_book_context(raw_url)
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
                    download_chapters(data, chapters, book_dir)
                    build_epub(data, chapters, book_dir, cover_bytes=cover_bytes, cover_ext=cover_ext)
                    break
                if choice == "2":
                    start = _ask_int("Chuong bat dau: ")
                    end = _ask_int("Chuong ket thuc: ", len(chapters))
                    start, end = _normalize_range(len(chapters), start, end)
                    download_chapters(data, chapters, book_dir, start=start, end=end)
                    break
                if choice == "3":
                    idx = _ask_int("Chuong can tai: ")
                    idx, _ = _normalize_range(len(chapters), idx, idx)
                    download_chapters(data, chapters, book_dir, start=idx, end=idx)
                    break
                if choice == "4":
                    start = _ask_int("Chuong bat dau [1]: ", 1)
                    end = _ask_int(f"Chuong ket thuc [{len(chapters)}]: ", len(chapters))
                    start, end = _normalize_range(len(chapters), start, end)
                    build_epub(data, chapters, book_dir, start=start, end=end, cover_bytes=cover_bytes, cover_ext=cover_ext)
                    break
                if choice == "5":
                    return
                _safe_print("Lua chon khong hop le.")
            except Exception as exc:
                _safe_print(f"Loi: {exc}")

        if not _post_task_menu():
            return


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Download truyencom.com chapters and build EPUB.")
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


if __name__ == "__main__":
    main()
