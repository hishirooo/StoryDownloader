# -*- coding: utf-8 -*-
"""
Downloader cho https://www.balshuzhal.cc/ (百书斋).

Trang mục lục mẫu:
  https://www.balshuzhal.cc/ibook/78540/78540008/

Trang chương mẫu:
  https://www.balshuzhal.cc/ibook/78540/78540008/28361328.html
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from uuid import uuid4
import html
import io
import re
import sys
import time
import zipfile
from download_logger import chapter_log_line
from epub_metadata import PUBLISHER, subject_xml

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


BASE_URL = "https://www.balshuzhal.cc/"
DEFAULT_URL = "https://www.balshuzhal.cc/ibook/78540/78540008/"
OUTPUT_BASE = Path("output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,vi;q=0.8,en;q=0.7",
    "Referer": BASE_URL,
}

TIMEOUT = 25
SLEEP_BETWEEN_PAGES = 0.8
SLEEP_BETWEEN_CHAPS = 1.0
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
    value = value.replace("\xa0", " ")
    value = value.replace("\u3000", " ")
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

    apparent = getattr(response, "apparent_encoding", None) if response is not None else None
    if apparent:
        candidates.append(apparent)

    candidates.extend(["gb18030", "gbk", "utf-8"])
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
    return "gb18030"


def _decode_html(content: bytes, response=None) -> str:
    return content.decode(_detect_encoding(content, response), errors="replace")


class FetchHtmlError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _fetch_html_with_status(url: str, tries: int = 3, backoff: float = 0.8) -> Tuple[BeautifulSoup, int]:
    last_error: Optional[Exception] = None
    last_status: Optional[int] = None

    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(backoff * attempt)
        try:
            response = _http_get(url)
            status_code = getattr(response, "status_code", 200)
            last_status = status_code
            if status_code in RETRY_STATUS and attempt < tries:
                continue
            response.raise_for_status()
            content = getattr(response, "content", b"")
            if not content:
                text = getattr(response, "text", "")
                content = text.encode(getattr(response, "encoding", "gb18030") or "gb18030", errors="replace")
            return BeautifulSoup(_decode_html(content, response), "html.parser"), status_code
        except Exception as exc:
            last_error = exc
            response = getattr(exc, "response", None)
            if response is not None and getattr(response, "status_code", None):
                last_status = getattr(response, "status_code")
            if attempt >= tries:
                break

    raise FetchHtmlError(f"Không tải được HTML: {url} ({last_error})", last_status)


def _fetch_html(url: str, tries: int = 3, backoff: float = 0.8) -> BeautifulSoup:
    soup, _ = _fetch_html_with_status(url, tries=tries, backoff=backoff)
    return soup


def _read_local_html(path: str | Path) -> BeautifulSoup:
    content = Path(path).read_bytes()
    return BeautifulSoup(_decode_html(content), "html.parser")


def _get_book_info(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, str]:
    title = (
        _meta_content(soup, "og:novel:book_name", "og:title")
        or _text(soup.select_one("#info h1"))
        or _text(soup.find("h1"))
    )
    if not title:
        title_tag = _text(soup.find("title"))
        title = re.split(r"最新章节|无弹窗|,|-", title_tag, maxsplit=1)[0].strip()

    author = _meta_content(soup, "og:novel:author")
    status = _meta_content(soup, "og:novel:status")
    update_time = _meta_content(soup, "og:novel:update_time")
    category = _meta_content(soup, "og:novel:category")
    latest_chapter = _meta_content(soup, "og:novel:latest_chapter_name")
    latest_url = _meta_content(soup, "og:novel:latest_chapter_url")

    info_node = soup.select_one("#info")
    if info_node:
        for p in info_node.find_all("p"):
            line = _clean_spaces(p.get_text(" ", strip=True))
            if not author and "作者" in line:
                author = _clean_spaces(line.split("：", 1)[-1])
            elif not status and "状态" in line:
                status = _clean_spaces(line.split("：", 1)[-1].split(" ", 1)[0])
            elif not update_time and "最后更新" in line:
                update_time = _clean_spaces(line.split("：", 1)[-1])
            elif not latest_chapter and "最新章节" in line:
                latest_chapter = _clean_spaces(line.split("：", 1)[-1])
                link = p.find("a", href=True)
                if link:
                    latest_url = _absolute_url(page_url, link["href"])

    cover_url = _meta_content(soup, "og:image")
    if not cover_url:
        img = soup.select_one("#fmimg img[src]") or soup.select_one("#sidebar img[src]") or soup.find("img", src=True)
        if img:
            src = img.get("src", "").strip()
            if src and not src.startswith("data:"):
                cover_url = src
    if cover_url:
        cover_url = _absolute_url(page_url, cover_url)

    intro = _meta_content(soup, "og:description")
    intro_node = soup.select_one("#intro")
    if intro_node:
        intro_clone = BeautifulSoup(str(intro_node), "html.parser")
        for node in intro_clone.find_all(["script", "style"]):
            node.decompose()
        intro_text = intro_clone.get_text("\n", strip=True)
        intro_text = re.sub(r"各位书友要是觉得.*$", "", intro_text, flags=re.S).strip()
        if intro_text:
            intro = _clean_spaces(intro_text)

    if latest_url:
        latest_url = _absolute_url(page_url, latest_url)

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


def _chapter_number(title: str) -> Optional[int]:
    match = re.search(r"第\s*(\d+)\s*章", title or "")
    if match:
        return int(match.group(1))
    return None


def _is_chapter_url(page_url: str, url: str) -> bool:
    page = urlparse(page_url)
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != page.netloc:
        return False
    if not re.search(r"\.html?$", parsed.path, flags=re.I):
        return False

    page_dir = page.path if page.path.endswith("/") else page.path.rsplit("/", 1)[0] + "/"
    if not parsed.path.startswith(page_dir):
        return False
    return re.fullmatch(r"\d+\.html?", parsed.path.rsplit("/", 1)[-1], flags=re.I) is not None


def _get_list_chapters(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    list_root = soup.select_one(".listmain dl") or soup.select_one(".listmain")
    if not list_root:
        list_root = soup

    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()
    in_main_volume = False
    found_volume_header = False

    for node in list_root.find_all(["dt", "dd"], recursive=False):
        if node.name == "dt":
            dt_text = _clean_spaces(_text(node))
            if any(marker in dt_text for marker in ("正文", "章节", "卷")) and "最新" not in dt_text:
                in_main_volume = True
                found_volume_header = True
            elif "最新" in dt_text and not found_volume_header:
                in_main_volume = False
            continue

        if found_volume_header and not in_main_volume:
            continue

        link = node.find("a", href=True)
        if not link:
            continue
        title = _clean_spaces(_text(link))
        full_url = _absolute_url(page_url, link["href"])
        if not title or not _is_chapter_url(page_url, full_url):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        chapters.append({"title": title, "url": full_url})

    if not chapters:
        for a in list_root.find_all("a", href=True):
            title = _clean_spaces(_text(a))
            full_url = _absolute_url(page_url, a["href"])
            if not title or not _is_chapter_url(page_url, full_url) or full_url in seen:
                continue
            seen.add(full_url)
            chapters.append({"title": title, "url": full_url})

    # Nếu chỉ lấy được block mới nhất, sort theo số chương để trả về thứ tự đọc tự nhiên.
    if chapters and all(_chapter_number(chapter["title"]) is not None for chapter in chapters):
        chapters.sort(key=lambda chapter: _chapter_number(chapter["title"]) or 0)

    return chapters


def _find_catalog_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> Optional[str]:
    index_match = re.search(r'var\s+index_page\s*=\s*["\']([^"\']+)["\']', str(soup), flags=re.I)
    if index_match:
        return _absolute_url(chapter_url, index_match.group(1))

    for a in soup.select(".page_chapter a[href], .path a[href], a[href]"):
        text = _clean_spaces(_text(a))
        if text in {"返回目录", "目录"} or "目录" in text:
            return _absolute_url(chapter_url, a.get("href", ""))
    return None


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
    title = _text(soup.select_one(".content h1") or soup.find("h1"))
    if not title:
        title_tag = _text(soup.find("title"))
        if title_tag:
            parts = [part.strip() for part in title_tag.split("-") if part.strip()]
            if len(parts) >= 2:
                title = parts[1]

    title = _clean_spaces(title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title):].strip(" -:：")
    title = re.sub(r"\s*-?\s*无弹窗.*$", "", title).strip(" -:：")
    title = re.sub(r"\s*-?\s*百书斋.*$", "", title).strip(" -:：")
    return title or fallback or "Chương"


def _clean_chapter_text(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ")
    raw_text = raw_text.replace("\u3000", " ")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    trash_patterns = re.compile(
        r"(百书斋|balshuzhal\.cc|m\.balshuzhal\.cc|www\.balshuzhal\.cc|上一章|下一章|返回目录|"
        r"加入书签|章节错误|点击举报|举报后请耐心等待|手机站|手机版阅读|推荐阅读|小说相关推荐|"
        r"Copyright|All Rights Reserved|app2|read2|read3|chaptererror|posterror)",
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
        paragraphs.append(line)
    return paragraphs


def _get_chapter_content_html(soup: BeautifulSoup, title: str = "") -> str:
    content = soup.select_one("#content.showtxt") or soup.select_one("#content") or soup.select_one(".showtxt")
    if not content:
        candidates = [
            node for node in soup.select(".content, article, .reader")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 120
        ]
        content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not content:
        return "<p>(Không có nội dung)</p>"

    content = BeautifulSoup(str(content), "html.parser")
    root = content.select_one("#content") or content
    for node in root.find_all(["script", "style", "ins", "iframe", "select", "input"]):
        node.decompose()
    for node in root.select(".link, .page_chapter, #page_set"):
        node.decompose()
    for report in root.find_all("div"):
        if "章节错误" in _text(report) or "点击举报" in _text(report):
            report.decompose()

    for br in root.find_all("br"):
        br.replace_with("\n")

    paragraphs = _clean_chapter_text(root.get_text("\n", strip=False), title=title)
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
    last_status: Optional[int] = None
    for attempt in range(1, retries + 1):
        try:
            soup, status_code = _fetch_html_with_status(url)
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
                time.sleep(SLEEP_BETWEEN_CHAPS * attempt)
        except Exception:
            if attempt < retries:
                time.sleep(SLEEP_BETWEEN_CHAPS * attempt)

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
  <article class="chapter-content">
{content_html}
  </article>
  {source}
</body>
</html>
"""


def _read_cached_chapter(html_path: Path) -> Dict[str, str]:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    title = _text(soup.find("h1")) or _text(soup.find("title")) or html_path.stem
    article = soup.select_one("article.chapter-content") or soup.select_one("article") or soup.find("body") or soup
    for node in article.select(".source"):
        node.decompose()
    return {
        "title": title,
        "content_html": "\n".join(str(child) for child in article.contents).strip(),
        "url": str(html_path),
        "status_code": "CACHE",
    }


def _chapter_html_path(book_dir: Path, idx: int) -> Path:
    return book_dir / f"chapter_{idx:04d}.html"


def _legacy_chapter_html_path(book_dir: Path, idx: int) -> Path:
    return book_dir / "html" / f"{idx:04d}.html"


def _find_cached_chapter_path(book_dir: Path, idx: int) -> Optional[Path]:
    for path in (_chapter_html_path(book_dir, idx), _legacy_chapter_html_path(book_dir, idx)):
        if path.exists():
            return path
    for pattern in (f"{idx:04d} - *.html", f"{idx:04d}.html"):
        matches = sorted(book_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def _save_chapter_txt(chapter_data: Dict[str, str], txt_path: Path) -> None:
    soup = BeautifulSoup(chapter_data.get("content_html", ""), "html.parser")
    text = chapter_data.get("text") or soup.get_text("\n", strip=True)
    txt_path.write_text(f"{chapter_data['title']}\n\n{text}\n", encoding="utf-8")


def _save_chapter_html(
    chapter: Dict[str, str],
    idx: int,
    book_dir: Path,
    *,
    book_title: str = "",
    force: bool = False,
) -> Dict[str, str]:
    book_dir.mkdir(parents=True, exist_ok=True)
    txt_dir = book_dir / "txt"
    txt_dir.mkdir(parents=True, exist_ok=True)

    html_path = _chapter_html_path(book_dir, idx)
    txt_path = txt_dir / f"{idx:04d}.txt"
    cached_path = _find_cached_chapter_path(book_dir, idx)
    if cached_path and not force:
        data = _read_cached_chapter(cached_path)
        if cached_path != html_path:
            html_path.write_text(cached_path.read_text(encoding="utf-8"), encoding="utf-8")
        if not txt_path.exists():
            _save_chapter_txt(data, txt_path)
        return data

    data = fetch_chapter_content(
        chapter["url"],
        fallback_title=chapter.get("title", f"Chương {idx}"),
        book_title=book_title,
    )
    html_path.write_text(_chapter_html_doc(data["title"], data["content_html"], data["url"]), encoding="utf-8")
    _save_chapter_txt(data, txt_path)
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
    book_dir: Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
    force: bool = False,
) -> List[Dict[str, str]]:
    start, end = _normalize_range(len(chapters), start, end)
    total_selected = end - start + 1
    _safe_print(f"Bắt đầu tải/cache {total_selected} chương vào: {book_dir}")
    downloaded: List[Dict[str, str]] = []
    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        data = _save_chapter_html(
            chapter,
            idx,
            book_dir,
            book_title=book_info.get("title", ""),
            force=force,
        )
        downloaded.append(data)
        status_code = data.get("status_code", "ERR")
        _safe_print(chapter_log_line(done, total_selected, status_code, idx, len(chapters), chapter.get("title", "")))
    _safe_print(f"Hoàn tất tải/cache {total_selected} chương.")
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
    """API tương thích main.py: lưu HTML ở dạng 0001 - title.html trong out_dir."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    start, end = _normalize_range(len(chapters), start, end)
    total_selected = end - start + 1
    saved: List[Dict[str, str]] = []

    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        root_matches = sorted(out_path.glob(f"{idx:04d}*.html"))
        if root_matches and not force:
            data = _read_cached_chapter(root_matches[0])
        else:
            data = _save_chapter_html(chapter, idx, out_path, book_title=book_title, force=force)
            export_path = out_path / f"{idx:04d} - {_safe_filename(data.get('title') or chapter.get('title') or f'Chương {idx}', 90)}.html"
            if force or not export_path.exists():
                export_path.write_text(
                    _chapter_html_doc(data["title"], data["content_html"], data.get("url", chapter["url"])),
                    encoding="utf-8",
                )
        saved.append(data)
        _safe_print(chapter_log_line(done, total_selected, data.get("status_code", "ERR"), idx, len(chapters), data.get("title") or chapter.get("title") or ""))

    return saved


def save_combined_txt(book_info: Dict[str, str], chapters: List[Dict[str, str]], book_dir: Path) -> Path:
    book_dir.mkdir(parents=True, exist_ok=True)
    out_path = book_dir / f"{_safe_filename(book_info['title'])}.txt"
    chunks: List[str] = [book_info["title"], f"作者：{book_info.get('author', 'Unknown')}", ""]
    if book_info.get("intro"):
        chunks.extend(["内容简介：", book_info["intro"], ""])

    for idx, chapter in enumerate(chapters, 1):
        html_path = _find_cached_chapter_path(book_dir, idx)
        if html_path:
            data = _read_cached_chapter(html_path)
        else:
            data = _save_chapter_html(chapter, idx, book_dir, book_title=book_info.get("title", ""))
        text = BeautifulSoup(data["content_html"], "html.parser").get_text("\n", strip=True)
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


def _zip_write(zf: zipfile.ZipFile, arcname: str, data: bytes | str, *, compress: bool = True) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    info = zipfile.ZipInfo(arcname)
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    zf.writestr(info, data)


def _xhtml_page(title: str, body_html: str, *, css_href: str = "../Styles/style.css") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="{css_href}"/>
</head>
<body>
{body_html}
</body>
</html>
"""


def build_epub_manual(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
    cover_bytes: Optional[bytes] = None,
    cover_ext: Optional[str] = None,
) -> Path:
    start, end = _normalize_range(len(chapters), start, end)
    book_dir.mkdir(parents=True, exist_ok=True)

    items: List[Dict[str, str]] = []
    for idx in range(start, end + 1):
        html_path = _find_cached_chapter_path(book_dir, idx)
        if not html_path:
            _save_chapter_html(chapters[idx - 1], idx, book_dir, book_title=book_info.get("title", ""))
            html_path = _chapter_html_path(book_dir, idx)
        items.append(_read_cached_chapter(html_path))

    title = book_info.get("title") or "Truyện"
    author = book_info.get("author") or "Unknown"
    range_suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    epub_path = OUTPUT_BASE / f"{_safe_filename(title)}_{_safe_filename(author)}{range_suffix}.epub"
    epub_path.parent.mkdir(parents=True, exist_ok=True)
    uid = f"urn:uuid:{uuid4()}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    has_cover = bool(cover_bytes and cover_ext)
    cover_ext = (cover_ext or ".jpg").lower()
    if cover_ext == ".jpeg":
        cover_ext = ".jpg"
    cover_media = {
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(cover_ext, "image/jpeg")
    cover_name = f"Images/cover{cover_ext if cover_ext in {'.jpg', '.png', '.webp', '.gif'} else '.jpg'}"

    manifest_items: List[str] = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="style" href="Styles/style.css" media-type="text/css"/>',
        '<item id="titlepage" href="Text/title.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_items: List[str] = ['<itemref idref="titlepage"/>']
    nav_links: List[str] = ['<li><a href="Text/title.xhtml">封面</a></li>']
    nav_points: List[str] = [
        '<navPoint id="nav0" playOrder="1"><navLabel><text>封面</text></navLabel>'
        '<content src="Text/title.xhtml"/></navPoint>'
    ]

    if has_cover:
        manifest_items.append(f'<item id="cover-image" href="{cover_name}" media-type="{cover_media}" properties="cover-image"/>')
        manifest_items.append('<item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
        spine_items.insert(0, '<itemref idref="cover"/>')

    for order, chapter in enumerate(items, 1):
        file_name = f"Text/chapter_{order:04d}.xhtml"
        manifest_items.append(f'<item id="chap{order}" href="{file_name}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="chap{order}"/>')
        nav_links.append(f'<li><a href="{file_name}">{html.escape(chapter["title"])}</a></li>')
        nav_points.append(
            f'<navPoint id="nav{order}" playOrder="{order + 1}">'
            f'<navLabel><text>{html.escape(chapter["title"])}</text></navLabel>'
            f'<content src="{file_name}"/></navPoint>'
        )

    style_css = """
body { font-family: serif; line-height: 1.75; margin: 5%; }
h1 { font-size: 1.35em; line-height: 1.3; margin: 0 0 1em; text-align: center; }
p { margin: 0.65em 0; text-indent: 2em; }
.meta, .intro { text-indent: 0; }
.cover { text-align: center; margin: 0; text-indent: 0; }
.cover img { max-width: 100%; max-height: 95vh; height: auto; }
nav ol { padding-left: 1.4em; }
""".strip()

    intro = book_info.get("intro", "")
    title_body = [
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="meta">作者：{html.escape(author)}</p>',
    ]
    if intro:
        title_body.append(f'<p class="intro">{html.escape(intro)}</p>')
    title_xhtml = _xhtml_page(title, "\n".join(title_body))

    nav_xhtml = _xhtml_page(
        "目录",
        f"""<nav epub:type="toc" id="toc">
  <h1>目录</h1>
  <ol>
    {"".join(nav_links)}
  </ol>
</nav>""",
        css_href="Styles/style.css",
    )

    toc_ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{html.escape(uid)}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(title)}</text></docTitle>
  <navMap>{"".join(nav_points)}</navMap>
</ncx>
"""

    subjects = subject_xml(book_info.get("category", ""))
    content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="BookId">{html.escape(uid)}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>{html.escape(author)}</dc:creator>
    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>
{subjects}    <dc:language>zh-CN</dc:language>
    <dc:source>{html.escape(book_info.get("url", ""))}</dc:source>
    <meta property="dcterms:modified">{modified}</meta>
    {'<meta name="cover" content="cover-image"/>' if has_cover else ''}
  </metadata>
  <manifest>
    {"".join(manifest_items)}
  </manifest>
  <spine toc="ncx">
    {"".join(spine_items)}
  </spine>
</package>
"""

    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    _safe_print(f"Đang đóng gói EPUB: {epub_path}")
    with zipfile.ZipFile(epub_path, "w") as zf:
        _zip_write(zf, "mimetype", "application/epub+zip", compress=False)
        _zip_write(zf, "META-INF/container.xml", container_xml)
        _zip_write(zf, "OEBPS/Styles/style.css", style_css)
        _zip_write(zf, "OEBPS/content.opf", content_opf)
        _zip_write(zf, "OEBPS/toc.ncx", toc_ncx)
        _zip_write(zf, "OEBPS/nav.xhtml", nav_xhtml)
        _zip_write(zf, "OEBPS/Text/title.xhtml", title_xhtml)

        if has_cover:
            _zip_write(zf, f"OEBPS/{cover_name}", cover_bytes)
            cover_xhtml = _xhtml_page("Cover", f'<p class="cover"><img src="../{cover_name}" alt="{html.escape(title)}"/></p>')
            _zip_write(zf, "OEBPS/Text/cover.xhtml", cover_xhtml)

        for order, chapter in enumerate(items, 1):
            body = f"<h1>{html.escape(chapter['title'])}</h1>\n{chapter.get('content_html') or '<p>(Không có nội dung)</p>'}"
            _zip_write(zf, f"OEBPS/Text/chapter_{order:04d}.xhtml", _xhtml_page(chapter["title"], body))

    _safe_print(f"Đã tạo EPUB thủ công: {epub_path}")
    return epub_path


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


def _ask_int(prompt: str, default: Optional[int] = None) -> int:
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        try:
            return int(raw)
        except ValueError:
            _safe_print("Vui lòng nhập số hợp lệ.")


def _load_book_context(url: str) -> Tuple[Dict[str, str], List[Dict[str, str]], Path, Optional[bytes], Optional[str]]:
    url = _ensure_url(url)
    _safe_print("Đang lấy thông tin truyện...")
    data = getText(url)
    book_info = {
        "title": data["title"],
        "author": data["author"],
        "status": data.get("status", ""),
        "category": data.get("category", ""),
        "update_time": data.get("update_time", ""),
        "latest_chapter": data.get("latest_chapter", ""),
        "latest_chapter_url": data.get("latest_chapter_url", ""),
        "intro": data.get("intro", ""),
        "cover_url": data.get("cover_url", ""),
        "url": data.get("url", url),
    }
    chapters = data["chapters"]
    book_dir = _prepare_book_dir(book_info)
    _save_book_info(book_info, chapters, book_dir)
    epub_preview_path = OUTPUT_BASE / f"{_safe_filename(book_info['title'])}_{_safe_filename(book_info['author'])}.epub"

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
    _safe_print(f"Thư mục chương: {book_dir}")
    _safe_print(f"EPUB sẽ lưu  : {epub_preview_path}")
    if book_info.get("intro"):
        _safe_print(f"Giới thiệu   : {book_info['intro'][:160]}{'...' if len(book_info['intro']) > 160 else ''}")

    cover_bytes, cover_ext = _load_cover(book_info, book_dir)
    return book_info, chapters, book_dir, cover_bytes, cover_ext


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
    _safe_print("Downloader balshuzhal.cc / 百书斋")
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
                    build_epub_manual(book_info, chapters, book_dir, cover_bytes=cover_bytes, cover_ext=cover_ext)
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


if __name__ == "__main__":
    main()
