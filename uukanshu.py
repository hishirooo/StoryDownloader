# -*- coding: utf-8 -*-
"""
Downloader cho https://uukanshu.cc/.

Trang mục lục mẫu:
  https://uukanshu.cc/book/26782/

Trang chương mẫu:
  https://uukanshu.cc/book/26782/17359573.html
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
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


BASE_URL = "https://uukanshu.cc/"
DEFAULT_URL = "https://uukanshu.cc/book/26782/"
OUTPUT_BASE = Path("output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,zh-CN;q=0.8,vi;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Referer": BASE_URL,
}

TIMEOUT = 25
SLEEP_BETWEEN_PAGES = 0.8
SLEEP_BETWEEN_CHAPS = 1.0
CHAPTER_RETRIES = 5
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)


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

    candidates.extend(["utf-8", "big5", "gb18030", "gbk"])
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


def _get_book_info(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, str]:
    title = (
        _meta_content(soup, "og:novel:book_name", "og:title")
        or _text(soup.find("h1", class_="booktitle"))
        or _text(soup.select_one(".booktitle"))
        or _text(soup.find("h1"))
    )
    if not title:
        title_tag = _text(soup.find("title"))
        title = re.split(r"_|UU看書|UU看书|最新章節|最新章节", title_tag, maxsplit=1)[0].strip()

    author = _meta_content(soup, "og:novel:author")
    page_lines = soup.get_text("\n", strip=True)
    if not author:
        match = re.search(r"(?:作者|作\s*者)\s*[:：]\s*([^\n]+)", page_lines)
        if match:
            author = _clean_spaces(match.group(1))

    category = _meta_content(soup, "og:novel:category")
    marker = soup.find("span", class_="blue")
    if not category and marker:
        next_span = marker.find_next("span")
        if next_span:
            category = _text(next_span)

    status = _meta_content(soup, "og:novel:status")
    update_time = _meta_content(soup, "og:novel:update_time")
    latest_chapter = _meta_content(soup, "og:novel:latest_chapter_name")
    latest_url = _meta_content(soup, "og:novel:latest_chapter_url")
    if latest_url:
        latest_url = _absolute_url(page_url, latest_url)

    intro = _meta_content(soup, "og:description", "description")
    intro_node = soup.find("p", class_="bookintro") or soup.select_one(".bookintro, #bookintro, .intro")
    if intro_node:
        intro = _clean_spaces(intro_node.get_text("\n", strip=True))

    cover_url = _meta_content(soup, "og:image")
    if not cover_url:
        img = soup.find("img", class_="thumbnail") or soup.select_one(".book img[src], img.thumbnail[src]")
        if img:
            cover_url = img.get("src", "").strip()
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


def _book_id_from_url(url: str) -> Optional[str]:
    match = re.search(r"/book/(\d+)/", urlparse(url).path + "/")
    return match.group(1) if match else None


def _is_chapter_url(page_url: str, chapter_url: str) -> bool:
    page = urlparse(page_url)
    target = urlparse(chapter_url)
    if target.netloc and target.netloc != page.netloc:
        return False
    if not re.fullmatch(r"/book/\d+/\d+\.html?", target.path, flags=re.I):
        return False

    page_book_id = _book_id_from_url(page_url)
    target_book_id = _book_id_from_url(chapter_url)
    return not page_book_id or page_book_id == target_book_id


def _get_list_chapters(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    containers = [
        node for node in [
            soup.find("div", id="list-chapterAll"),
            soup.select_one("#list-chapterAll"),
            soup.select_one(".list-chapterAll"),
            soup.select_one(".chapter-list"),
            soup.select_one(".listmain"),
        ]
        if node is not None
    ]
    if not containers:
        containers = [soup]

    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    for container in containers:
        for a in container.find_all("a", href=True):
            title = _clean_spaces(_text(a))
            url = _absolute_url(page_url, a.get("href", ""))
            if not title or not _is_chapter_url(page_url, url):
                continue
            if url in seen:
                continue
            seen.add(url)
            chapters.append({"title": title, "url": url})

    return chapters


def _find_catalog_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> Optional[str]:
    parsed = urlparse(chapter_url)
    match = re.match(r"(?P<book_dir>/book/\d+)/\d+\.html?$", parsed.path, flags=re.I)
    if match:
        return f"{parsed.scheme or 'https'}://{parsed.netloc}{match.group('book_dir')}/"

    for a in soup.select("a[href]"):
        text = _clean_spaces(_text(a))
        if text in {"目錄", "目录", "返回目錄", "返回目录"} or "目錄" in text or "目录" in text:
            return _absolute_url(chapter_url, a.get("href", ""))
    return None


def _chapter_referer(url: str) -> str:
    parsed = urlparse(url)
    match = re.match(r"(?P<book_dir>/book/\d+)/\d+\.html?$", parsed.path, flags=re.I)
    if match:
        return f"{parsed.scheme or 'https'}://{parsed.netloc}{match.group('book_dir')}/"
    return BASE_URL


def _retry_delay_seconds(status_code: Optional[int], attempt: int) -> float:
    if status_code == 403:
        return min(14.0, 4.0 + attempt * 2.0)
    if status_code == 429:
        return min(20.0, 5.0 * attempt)
    return max(SLEEP_BETWEEN_CHAPS, 1.5 * attempt)


def getText(url: str) -> Dict:
    url = _ensure_url(url)
    soup = _fetch_html(url)

    chapters = _get_list_chapters(soup, url)
    if not chapters:
        catalog_url = _find_catalog_url_from_chapter(soup, url)
        if catalog_url and catalog_url != url:
            url = catalog_url
            soup = _fetch_html(url)
            chapters = _get_list_chapters(soup, url)

    info = _get_book_info(soup, url)
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
        "cover_url": info["cover_url"],
        "intro": info["intro"],
        "url": url,
    }


def _chapter_title_from_page(soup: BeautifulSoup, book_title: str = "", fallback: str = "") -> str:
    title = _text(soup.select_one(".readtitle h1") or soup.select_one(".chapter-title") or soup.find("h1"))
    if not title:
        title_tag = _text(soup.find("title"))
        parts = [part.strip() for part in re.split(r"[_\-]", title_tag) if part.strip()]
        if parts:
            title = parts[0]

    title = _clean_spaces(title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title):].strip(" -_:：")
    title = re.sub(r"\s*[-_]?.*?UU看書.*$", "", title, flags=re.I).strip(" -_:：")
    title = re.sub(r"\s*[-_]?.*?UU看书.*$", "", title, flags=re.I).strip(" -_:：")
    return title or fallback or "Chương"


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ").replace("\u3000", " ")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    trash_patterns = re.compile(
        r"(UU看書|UU看书|uukanshu\.cc|www\.uukanshu|上一章|下一章|返回目錄|返回目录|目錄|目录|"
        r"加入書架|加入书架|書籤|书签|廣告|广告|手機版|手机版|电脑版|本書首發|本书首发|"
        r"本站|版權|版权|Copyright|loadAdv|chaptererror|posterror)",
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
        if len(line) < 10 and not re.search(r"[a-zA-Z0-9\u4e00-\u9fff]", line):
            continue
        paragraphs.append(line)
    return paragraphs


def _get_chapter_content_html(soup: BeautifulSoup, title: str = "") -> str:
    content = (
        soup.select_one("div.readcotent.bbb.font-normal")
        or soup.select_one(".readcotent")
        or soup.select_one(".readcontent")
        or soup.select_one(".read-content")
        or soup.select_one("#content")
        or soup.select_one("article")
    )
    if not content:
        candidates = [
            node for node in soup.select(".content, .chapter-content, .reader, .book-content")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 120
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return "<p>(Không có nội dung)</p>"

    content = BeautifulSoup(str(content), "html.parser")
    for node in content.find_all(["script", "style", "ins", "iframe", "select", "input"]):
        node.decompose()
    for node in content.select(".ads, .ad, .readad, .chapter-nav, .pager, .page"):
        node.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")

    paragraphs = _clean_chapter_lines(content.get_text("\n", strip=False), title=title)
    if not paragraphs:
        return "<p>(Không có nội dung)</p>"
    return "\n".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)


def fetch_chapter_content(
    url: str,
    retries: int = CHAPTER_RETRIES,
    *,
    fallback_title: str = "",
    book_title: str = "",
) -> Dict:
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
                time.sleep(_retry_delay_seconds(last_status, attempt))
        except Exception:
            if attempt < retries:
                time.sleep(_retry_delay_seconds(last_status, attempt))

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
    content_html = "\n".join(str(child) for child in article.contents).strip()
    if not content_html:
        content_html = "<p>(Không có nội dung)</p>"
    text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
    status_code = "ERR_CACHE" if _looks_like_failed_content(text) else "CACHE"
    return {
        "title": title,
        "content_html": content_html,
        "text": text,
        "url": str(html_path),
        "status_code": status_code,
    }


def _chapter_html_path(book_dir: str | Path, idx: int) -> Path:
    return Path(book_dir) / "html" / f"{idx:04d}.html"


def _chapter_export_path(book_dir: str | Path, idx: int, title: str) -> Path:
    return Path(book_dir) / f"{idx:04d} - {_safe_filename(title, 90)}.html"


def _find_cached_chapter_path(book_dir: str | Path, idx: int) -> Optional[Path]:
    directory = Path(book_dir)
    for pattern in (f"{idx:04d} - *.html", f"{idx:04d}.html", f"chapter_{idx:04d}.html"):
        matches = sorted(directory.glob(pattern))
        if matches:
            return matches[0]

    html_dir = directory / "html"
    if html_dir.is_dir():
        for pattern in (f"{idx:04d}.html", f"{idx:04d} - *.html", f"chapter_{idx:04d}.html"):
            matches = sorted(html_dir.glob(pattern))
            if matches:
                return matches[0]
    return None


def _looks_like_failed_content(text: str) -> bool:
    normalized = _clean_spaces(text)
    return any(
        marker in normalized
        for marker in (
            "Không tải được nội dung",
            "Nội dung không tải được",
            "Không có nội dung",
        )
    )


def _is_failed_chapter_data(data: Dict[str, str]) -> bool:
    status = data.get("status_code")
    if isinstance(status, int) and status >= 400:
        return True
    if status in {"ERR", "ERR_CACHE"}:
        return True
    text = data.get("text") or BeautifulSoup(data.get("content_html", ""), "html.parser").get_text("\n", strip=True)
    return _looks_like_failed_content(text)


def _cached_file_is_failed(path: Path) -> bool:
    try:
        return _is_failed_chapter_data(_read_cached_chapter(path))
    except Exception:
        return True


def _save_chapter_txt(data: Dict[str, str], txt_path: Path) -> None:
    soup = BeautifulSoup(data.get("content_html", ""), "html.parser")
    text = data.get("text") or soup.get_text("\n", strip=True)
    txt_path.write_text(f"{data.get('title', txt_path.stem)}\n\n{text}\n", encoding="utf-8")


def _write_export_html(data: Dict[str, str], book_dir: Path, idx: int, source_url: str = "") -> Path:
    export_path = _chapter_export_path(book_dir, idx, data.get("title") or f"Chương {idx}")
    if not export_path.exists() or _cached_file_is_failed(export_path):
        export_path.write_text(
            _chapter_html_doc(data["title"], data["content_html"], data.get("url") or source_url),
            encoding="utf-8",
        )
    return export_path


def _save_chapter_html(
    chapter: Dict[str, str],
    idx: int,
    book_dir: str | Path,
    *,
    book_title: str = "",
    force: bool = False,
) -> Dict[str, str]:
    out_path = Path(book_dir)
    html_dir = out_path / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    html_path = _chapter_html_path(out_path, idx)

    cached_path = _find_cached_chapter_path(out_path, idx)
    if cached_path and not force:
        data = _read_cached_chapter(cached_path)
        if not _is_failed_chapter_data(data):
            if cached_path != html_path and not html_path.exists():
                html_path.write_text(
                    _chapter_html_doc(data["title"], data["content_html"], data.get("url", chapter["url"])),
                    encoding="utf-8",
                )
            return data

    data = fetch_chapter_content(
        chapter["url"],
        fallback_title=chapter.get("title", f"Chương {idx}"),
        book_title=book_title,
    )
    if not _is_failed_chapter_data(data):
        html_path.write_text(_chapter_html_doc(data["title"], data["content_html"], data["url"]), encoding="utf-8")
        data["html_path"] = str(html_path)
    elif html_path.exists() and _cached_file_is_failed(html_path):
        try:
            html_path.unlink()
        except OSError:
            pass
    return data


def _normalize_range(total: int, start: int = 1, end: Optional[int] = None) -> Tuple[int, int]:
    if total <= 0:
        return 1, 0
    start = max(1, start)
    end = total if end is None else min(total, end)
    if end < start:
        raise ValueError("Khoảng chương không hợp lệ")
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

    _safe_print(f"Bắt đầu tải/cache {selected_total} chương vào: {book_dir}")
    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        data = _save_chapter_html(
            chapter,
            idx,
            book_dir,
            book_title=book_info.get("title", ""),
            force=force,
        )
        if _is_failed_chapter_data(data):
            failures.append((idx, data.get("status_code", "ERR")))
        else:
            _write_export_html(data, book_dir, idx, chapter.get("url", ""))
        downloaded.append(data)
        _safe_print(chapter_log_line(done, selected_total, data.get("status_code", "ERR"), idx, len(chapters), data.get("title") or chapter.get("title") or ""))

    _safe_print(f"Hoàn tất tải/cache {selected_total} chương.")
    if failures:
        sample = ", ".join(f"{idx}:{status}" for idx, status in failures[:8])
        suffix = "..." if len(failures) > 8 else ""
        _safe_print(f"Cảnh báo: còn {len(failures)} chương chưa tải được ({sample}{suffix}). Chạy lại sẽ tự thử tải lại cache lỗi.")
    return downloaded


def save_all_chapters_to_html(
    book_title: str,
    chapters: List[Dict[str, str]],
    out_dir: str,
    start: int = 1,
    end: Optional[int] = None,
    *,
    force: bool = False,
) -> List[Dict[str, str]]:
    book_info = {"title": book_title or "Unknown"}
    return download_chapters(book_info, chapters, out_dir, start=start, end=end, force=force)


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
    total = end - start + 1

    failures: List[Tuple[int, object]] = []
    for idx in range(start, end + 1):
        cached = _find_cached_chapter_path(out_dir, idx)
        if cached:
            data = _read_cached_chapter(cached)
            if _is_failed_chapter_data(data):
                data = _save_chapter_html(chapters[idx - 1], idx, out_dir, book_title=book_info.get("title", ""))
        else:
            data = _save_chapter_html(chapters[idx - 1], idx, out_dir, book_title=book_info.get("title", ""))
        if _is_failed_chapter_data(data):
            failures.append((idx, data.get("status_code", "ERR")))
        else:
            _save_chapter_txt(data, txt_dir / f"{idx:04d}.txt")
    _safe_print(f"Đã lưu TXT tách chương: {txt_dir}")
    if failures:
        sample = ", ".join(f"{idx}:{status}" for idx, status in failures[:8])
        suffix = "..." if len(failures) > 8 else ""
        _safe_print(f"TXT tách bỏ qua {len(failures)} chương lỗi ({sample}{suffix})")


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

    total = end - start + 1
    failures: List[Tuple[int, object]] = []
    for idx in range(start, end + 1):
        cached = _find_cached_chapter_path(book_dir, idx)
        if cached:
            data = _read_cached_chapter(cached)
            if _is_failed_chapter_data(data):
                data = _save_chapter_html(chapters[idx - 1], idx, book_dir, book_title=book_info.get("title", ""))
        else:
            data = _save_chapter_html(chapters[idx - 1], idx, book_dir, book_title=book_info.get("title", ""))
        text = data.get("text") or BeautifulSoup(data["content_html"], "html.parser").get_text("\n", strip=True)
        if _is_failed_chapter_data(data):
            failures.append((idx, data.get("status_code", "ERR")))
        chunks.extend([data["title"], "", text, ""])

    out_path.write_text("\n".join(chunks), encoding="utf-8")
    _safe_print(f"Đã lưu TXT gộp: {out_path}")
    if failures:
        sample = ", ".join(f"{idx}:{status}" for idx, status in failures[:8])
        suffix = "..." if len(failures) > 8 else ""
        _safe_print(f"TXT gộp còn {len(failures)} chương lỗi ({sample}{suffix})")
    return out_path


def _save_txt_combined(book_title: str, chapters: List[Dict], out_dir: str, start: int = 1, end: Optional[int] = None):
    return save_combined_txt({"title": book_title, "author": "Unknown"}, chapters, out_dir, start, end)


def _save_txt_split(book_title: str, chapters: List[Dict], out_dir: str, start: int = 1, end: Optional[int] = None):
    return save_txt_from_html({"title": book_title, "author": "Unknown"}, chapters, out_dir, start, end)


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
    chapters_data = _selected_chapter_data(book_info, chapters, book_dir, start=start, end=end)

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


def _save_epub(
    book_title: str,
    author: str,
    chapters: List[Dict],
    out_dir: str,
    cover_bytes: bytes = None,
    cover_ext: str = ".jpg",
    start: int = 1,
    end: Optional[int] = None,
):
    book_info = {"title": book_title or "Unknown", "author": author or "Unknown", "url": ""}
    return build_epub(book_info, chapters, out_dir, start=start, end=end, cover_bytes=cover_bytes, cover_ext=cover_ext)


def _selected_chapter_data(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: str | Path,
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
        if not _is_failed_chapter_data(data):
            _write_export_html(data, book_dir, idx, chapters[idx - 1].get("url", ""))
        items.append(data)
        status = data.get("status_code")
        if status not in ("CACHE", 200):
            failures.append((idx, status))

    if failures:
        sample = ", ".join(f"{idx}:{status}" for idx, status in failures[:5])
        suffix = "..." if len(failures) > 5 else ""
        _safe_print(f"[Epub] Cảnh báo: {len(failures)} chương lỗi ({sample}{suffix})")

    return items


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
        "Mục lục:",
    ]
    for idx, chapter in enumerate(chapters, 1):
        lines.append(f"{idx:04d}. {chapter['title']} - {chapter['url']}")
    (book_dir / "book_info.txt").write_text("\n".join(lines), encoding="utf-8")


def _load_cover(book_info: Dict[str, str], book_dir: Path) -> Tuple[Optional[bytes], Optional[str]]:
    cover_url = book_info.get("cover_url") or ""
    cover_bytes, cover_ext = _download_cover(cover_url)
    if cover_bytes and cover_ext:
        cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
        cover_path = book_dir / f"cover{cover_ext}"
        cover_path.write_bytes(cover_bytes)
        _safe_print(f"Đã lưu cover: {cover_path}")
    return cover_bytes, cover_ext


def _load_book_context(url: str) -> Tuple[Dict[str, str], List[Dict[str, str]], Path, Optional[bytes], Optional[str]]:
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
    book_dir = _prepare_book_dir(book_info)
    _save_book_info(book_info, chapters, book_dir)
    epub_preview_path = book_dir / f"{_safe_filename(book_info['title'])}.epub"

    _safe_print("\n-----------------Thông tin truyện-----------------")
    _safe_print(f"Tên truyện   : {book_info['title']}")
    _safe_print(f"Tác giả      : {book_info['author']}")
    if book_info.get("status"):
        _safe_print(f"Trạng thái   : {book_info['status']}")
    if book_info.get("category"):
        _safe_print(f"Thể loại     : {book_info['category']}")
    _safe_print(f"Số chương    : {len(chapters)}")
    if book_info.get("latest_chapter"):
        _safe_print(f"Mới nhất     : {book_info['latest_chapter']}")
    _safe_print(f"Thư mục truyện: {book_dir}")
    _safe_print(f"EPUB sẽ lưu  : {epub_preview_path}")
    if book_info.get("intro"):
        intro = book_info["intro"]
        _safe_print(f"Giới thiệu   : {intro[:160]}{'...' if len(intro) > 160 else ''}")

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
    _safe_print("Downloader uukanshu.cc / UU看書")
    while True:
        raw_url = input(f"Nhập Url [{DEFAULT_URL}]: ").strip() or DEFAULT_URL
        try:
            book_info, chapters, book_dir, cover_bytes, cover_ext = _load_book_context(raw_url)
        except Exception as exc:
            _safe_print(f"Lỗi: {exc}")
            continue

        while True:
            _print_download_menu()
            choice = input("Chọn [1]: ").strip() or "1"

            try:
                if choice == "1":
                    download_chapters(book_info, chapters, book_dir)
                    build_epub(book_info, chapters, book_dir, cover_bytes=cover_bytes, cover_ext=cover_ext)
                    break
                if choice == "2":
                    start = _ask_int("Chương bắt đầu: ")
                    end = _ask_int("Chương kết thúc: ", len(chapters))
                    start, end = _normalize_range(len(chapters), start, end)
                    download_chapters(book_info, chapters, book_dir, start=start, end=end)
                    break
                if choice == "3":
                    idx = _ask_int("Chương cần tải: ")
                    idx, _ = _normalize_range(len(chapters), idx, idx)
                    download_chapters(book_info, chapters, book_dir, start=idx, end=idx)
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
    main()
