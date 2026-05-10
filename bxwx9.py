# -*- coding: utf-8 -*-
"""
Downloader cho https://www.bxwx9.org/ (笔下文学网).

Trang mục lục mẫu:
  https://www.bxwx9.org/b/131/131364/

Trang chương mẫu:
  https://www.bxwx9.org/b/131/131364/428174.html
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import html
import io
import os
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


BASE_URL = "https://www.bxwx9.org/"
DEFAULT_URL = "https://www.bxwx9.org/b/131/131364/"
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

    candidates.extend(["utf-8", "gb18030", "gbk"])
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


def _fetch_html(url: str, tries: int = 3, backoff: float = 0.8) -> BeautifulSoup:
    soup, _ = _fetch_html_with_status(url, tries=tries, backoff=backoff)
    return soup


def _read_local_html(path: str | Path) -> BeautifulSoup:
    content = Path(path).read_bytes()
    return BeautifulSoup(_decode_html(content), "html.parser")


def _get_book_info(soup: BeautifulSoup, page_url: str = DEFAULT_URL) -> Dict[str, str]:
    title = (
        _meta_content(soup, "og:novel:book_name")
        or _text(soup.select_one(".book_info .info h1"))
        or _text(soup.find("h1"))
    )
    if not title:
        title_tag = _text(soup.find("title"))
        title = re.split(r"目录|最新章节|全文免费阅读|_", title_tag, maxsplit=1)[0].strip()

    author = _meta_content(soup, "og:novel:author")
    if not author:
        for li in soup.select(".book_info .options li"):
            text = _clean_spaces(li.get_text(" ", strip=True))
            if "作者" in text:
                author = _clean_spaces(text.split("：", 1)[-1])
                break

    status = _meta_content(soup, "og:novel:status")
    update_time = _meta_content(soup, "og:novel:update_time")
    category = _meta_content(soup, "og:novel:category")
    latest_title = _meta_content(soup, "og:novel:latest_chapter_name")
    latest_url = _meta_content(soup, "og:novel:latest_chapter_url")
    if latest_url:
        latest_url = _absolute_url(page_url, latest_url)

    if not status:
        for li in soup.select(".book_info .options li"):
            text = _clean_spaces(li.get_text(" ", strip=True))
            if "状态" in text:
                status = _clean_spaces(text.split("：", 1)[-1])
                break

    cover_url = _meta_content(soup, "og:image")
    if not cover_url:
        img = soup.select_one(".book_info img.img-thumbnail[src]") or soup.select_one(".book_info img[src]")
        if img:
            src = img.get("src", "").strip()
            if src and "_files/" not in src:
                cover_url = src
    if cover_url:
        cover_url = _absolute_url(page_url, cover_url)

    intro = ""
    intro_node = soup.select_one("#intro_pc") or soup.select_one(".intro")
    if intro_node:
        intro_clone = BeautifulSoup(str(intro_node), "html.parser")
        for node in intro_clone.find_all(["script", "style", "a", "span", "strong"]):
            if node.name == "strong":
                node.unwrap()
            else:
                node.decompose()
        intro = intro_clone.get_text("\n", strip=True)
        intro = re.sub(r"^\s*简介\s*[:：]\s*", "", intro)
        intro = re.sub(r"您要是觉得.*$", "", intro, flags=re.S).strip()
        intro = _clean_spaces(intro)

    total_chapters = 0
    page_text = _clean_spaces(soup.get_text(" ", strip=True))
    match = re.search(r"共\s*(\d+)\s*章", page_text)
    if match:
        total_chapters = int(match.group(1))

    return {
        "title": title or "Unknown",
        "author": author or "Unknown",
        "status": status or "",
        "category": category or "",
        "update_time": update_time or "",
        "latest_chapter": latest_title or "",
        "latest_chapter_url": latest_url or "",
        "cover_url": cover_url or "",
        "intro": intro or "",
        "total_chapters": total_chapters,
        "url": page_url,
    }


def _is_same_book_chapter(page_url: str, chapter_url: str) -> bool:
    page = urlparse(page_url)
    target = urlparse(chapter_url)
    if target.netloc and target.netloc != page.netloc:
        return False

    page_path = page.path
    if page_path.endswith("/"):
        book_dir = page_path
    else:
        book_dir = page_path.rsplit("/", 1)[0] + "/"

    basename = target.path.rsplit("/", 1)[-1].lower()
    return target.path.startswith(book_dir) and re.fullmatch(r"\d+\.html?", basename, flags=re.I) is not None


def _extract_chapters_from_catalog_page(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    containers = soup.select(".book_list.book_list2")
    if not containers:
        containers = [
            node for node in soup.select(".book_list")
            if "最新" not in _clean_spaces(node.find_previous("h2").get_text(" ", strip=True) if node.find_previous("h2") else "")
        ]

    for container in containers:
        for a in container.find_all("a", href=True):
            title = _clean_spaces(_text(a))
            url = _absolute_url(page_url, a.get("href", ""))
            if title and _is_same_book_chapter(page_url, url):
                chapters.append({"title": title, "url": url})
    return chapters


def _catalog_page_urls(soup: BeautifulSoup, page_url: str) -> List[str]:
    urls: List[str] = []
    seen: set[str] = set()
    parsed_page = urlparse(page_url)

    def add(url: str) -> None:
        parsed = urlparse(url)
        normalized = parsed._replace(fragment="", query="").geturl()
        if normalized in seen:
            return
        if parsed.netloc and parsed.netloc != parsed_page.netloc:
            return
        if not re.search(r"/index_\d+\.html?$", parsed.path, flags=re.I):
            return
        seen.add(normalized)
        urls.append(normalized)

    for a in soup.select(".pages a[href], .pagination a[href], a.page-link[href]"):
        add(_absolute_url(page_url, a.get("href", "")))
    return urls


def _get_list_chapters(soup: BeautifulSoup, page_url: str, fetch_extra_pages: bool = True) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()

    def add_many(items: List[Dict[str, str]]) -> None:
        for chapter in items:
            url = chapter["url"]
            if url in seen:
                continue
            seen.add(url)
            chapters.append(chapter)

    add_many(_extract_chapters_from_catalog_page(soup, page_url))

    if fetch_extra_pages:
        parsed_current = urlparse(page_url)._replace(fragment="", query="")
        current_path = parsed_current.geturl()
        current_dir = parsed_current.path if parsed_current.path.endswith("/") else parsed_current.path.rsplit("/", 1)[0] + "/"
        for extra_url in _catalog_page_urls(soup, page_url):
            if extra_url == current_path:
                continue
            extra_path = urlparse(extra_url).path
            if parsed_current.path.endswith("/") and extra_path == f"{current_dir}index_1.html":
                continue
            try:
                time.sleep(SLEEP_BETWEEN_PAGES)
                extra_soup = _fetch_html(extra_url)
                add_many(_extract_chapters_from_catalog_page(extra_soup, extra_url))
            except Exception as exc:
                _safe_print(f"⚠ Không tải được trang mục lục phụ {extra_url}: {exc}")

    return chapters


def _find_catalog_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> Optional[str]:
    for a in soup.select(".nav-bottom a[href], a[href]"):
        text = _clean_spaces(_text(a))
        if text == "目录":
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
    total = info.get("total_chapters") or len(chapters)
    return {
        "title": info["title"],
        "author": info["author"],
        "status": info.get("status", ""),
        "category": info.get("category", ""),
        "update_time": info.get("update_time", ""),
        "latest_chapter": info.get("latest_chapter", ""),
        "latest_chapter_url": info.get("latest_chapter_url", ""),
        "chapters": chapters,
        "total_chapters": total,
        "cover_url": info["cover_url"],
        "intro": info["intro"],
        "url": url,
    }


def _chapter_title_from_page(soup: BeautifulSoup, book_title: str = "", fallback: str = "") -> str:
    title = _text(soup.select_one(".box.single h1") or soup.select_one(".single h1") or soup.find("h1"))
    if not title:
        title_tag = _text(soup.find("title"))
        parts = [part.strip() for part in title_tag.split("_") if part.strip()]
        if len(parts) >= 3:
            title = parts[2]
        elif parts:
            title = parts[-1]

    title = _clean_spaces(title)
    title = re.sub(r"\s*[-_]\s*《.*?》\s*$", "", title)
    if book_title:
        title = title.replace(f"《{book_title}》", "").strip()
        title = title.removeprefix(book_title).strip(" -_：:")
    title = re.sub(r"_?笔下文学网$", "", title).strip(" -_：:")
    return title or fallback or "Chương"


def _chapter_page_count(soup: BeautifulSoup) -> int:
    article = soup.select_one(".box.single article") or soup.select_one("article")
    text = article.get_text("\n", strip=True) if article else soup.get_text("\n", strip=True)
    matches = re.findall(r"第\s*\(\s*\d+\s*/\s*(\d+)\s*\)\s*页", text)
    if not matches:
        return 1
    try:
        return max(1, max(int(value) for value in matches))
    except ValueError:
        return 1


def _chapter_part_urls(url: str, total_pages: int) -> List[str]:
    if total_pages <= 1:
        return [url]
    base_url = url.split("#", 1)[0].split("?", 1)[0]
    directory, filename = base_url.rsplit("/", 1)
    match = re.match(r"(?P<chapter_id>\d+)(?:_\d+)?\.html?$", filename, flags=re.I)
    if not match:
        return [url]
    chapter_id = match.group("chapter_id")
    return [
        f"{directory}/{chapter_id}.html" if page_no == 1 else f"{directory}/{chapter_id}_{page_no}.html"
        for page_no in range(1, total_pages + 1)
    ]


def _clean_chapter_lines(raw_text: str, title: str = "") -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    trash = re.compile(
        r"(笔下文学网|bxwx9\.org|www\.bxwx9|上一章|下一章|目录|存书签|加入书架|点击阅读|"
        r"追看新章节|下载本站客户端|广告|APP|addMark|ddfirst|ddtop|ddnext|footer)",
        flags=re.I,
    )

    lines: List[str] = []
    for raw_line in raw_text.split("\n"):
        line = _clean_spaces(raw_line)
        if not line:
            continue
        if title and line == title:
            continue
        if re.fullmatch(r"第\s*\(\s*\d+\s*/\s*\d+\s*\)\s*页", line):
            continue
        if trash.search(line):
            continue
        if len(line) < 2:
            continue
        lines.append(line)
    return lines


def _extract_chapter_paragraphs(soup: BeautifulSoup, title: str = "") -> List[str]:
    article = soup.select_one(".box.single article") or soup.select_one("article.font_max") or soup.select_one("article")
    if not article:
        candidates = [
            node for node in soup.select("#content, .content, .chapter-content, .read-content, .box.single")
            if len(_clean_spaces(node.get_text(" ", strip=True))) > 80
        ]
        article = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True)))) if candidates else None
    if not article:
        return []

    article = BeautifulSoup(str(article), "html.parser")
    for node in article.find_all(["script", "style", "ins", "iframe"]):
        node.decompose()
    for node in article.select(".nav-bottom, .ads, .ad, .layui-row"):
        node.decompose()

    for br in article.find_all("br"):
        br.replace_with("\n")

    return _clean_chapter_lines(article.get_text("\n", strip=False), title=title)


def _chapter_content_html_from_pages(pages: List[BeautifulSoup], title: str = "") -> str:
    paragraphs: List[str] = []
    for page in pages:
        paragraphs.extend(_extract_chapter_paragraphs(page, title=title))
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
            first_soup, status_code = _fetch_html_with_status(url)
            last_status = status_code
            title = _chapter_title_from_page(first_soup, book_title=book_title, fallback=fallback_title)
            total_pages = _chapter_page_count(first_soup)
            page_urls = _chapter_part_urls(url, total_pages)
            pages = [first_soup]

            for page_url in page_urls[1:]:
                try:
                    time.sleep(SLEEP_BETWEEN_PAGES)
                    page_soup = _fetch_html(page_url)
                    pages.append(page_soup)
                except Exception as exc:
                    _safe_print(f"⚠ Không tải được trang phụ {page_url}: {exc}")

            content_html = _chapter_content_html_from_pages(pages, title=title)
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
    for node in article.select(".source"):
        node.decompose()
    content_html = "\n".join(str(child) for child in article.contents).strip()
    return {"title": title, "content_html": content_html, "url": str(html_path), "status_code": "CACHE"}


def _chapter_html_path(out_dir: str | Path, idx: int, title: str) -> Path:
    return Path(out_dir) / f"{idx:04d} - {_safe_filename(title, 90)}.html"


def _find_cached_chapter_path(out_dir: str | Path, idx: int) -> Optional[Path]:
    directory = Path(out_dir)
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


def _status_label(status) -> str:
    if status == "CACHE":
        return "CACHE"
    if status == 200:
        return "\033[32mHTTP=200\033[0m"
    if isinstance(status, int):
        return f"\033[31mHTTP={status}\033[0m" if status >= 400 else f"HTTP={status}"
    return f"HTTP={status}"


def _normalize_range(total: int, start: int = 1, end: Optional[int] = None) -> Tuple[int, int]:
    if total <= 0:
        return 1, 0
    start = max(1, start)
    end = total if end is None else min(total, end)
    if end < start:
        raise ValueError("Khoảng chương không hợp lệ")
    return start, end


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
    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        data = _save_one_chapter_html(chapter, idx, out_dir, book_title=book_title, force=force)
        saved.append(data)
        _safe_print(
            f"[{done}/{selected_total}] [{_status_label(data.get('status_code', 'ERR'))}] "
            f"Chương {idx:04d}/{len(chapters):04d}: {data.get('title') or chapter.get('title')}"
        )
    return saved


def _save_chapter_txt(data: Dict[str, str], txt_path: Path) -> None:
    soup = BeautifulSoup(data.get("content_html", ""), "html.parser")
    text = data.get("text") or soup.get_text("\n", strip=True)
    txt_path.write_text(f"{data.get('title', txt_path.stem)}\n\n{text}\n", encoding="utf-8")


def save_txt_from_html(book_info: Dict[str, str], chapters: List[Dict[str, str]], out_dir: str, start: int = 1, end: Optional[int] = None) -> None:
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
    book_dir = OUTPUT_BASE / _safe_filename(book_info["title"])
    book_dir.mkdir(parents=True, exist_ok=True)
    _save_book_info(book_info, chapters, book_dir)

    _safe_print("\n-----------------Thông tin truyện-----------------")
    _safe_print(f"Tên truyện   : {book_info['title']}")
    _safe_print(f"Tác giả      : {book_info['author']}")
    if book_info.get("status"):
        _safe_print(f"Trạng thái   : {book_info['status']}")
    _safe_print(f"Số chương    : {len(chapters)}")
    if book_info.get("latest_chapter"):
        _safe_print(f"Mới nhất     : {book_info['latest_chapter']}")
    _safe_print(f"Thư mục HTML : {book_dir}")

    cover_bytes, cover_ext = None, None
    if book_info.get("cover_url"):
        _safe_print(f"[Cover] Đang tải cover: {book_info['title']}")
        cover_bytes, cover_ext = _download_cover(book_info["cover_url"])
        if cover_bytes and cover_ext:
            cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
            cover_path = book_dir / f"cover{cover_ext}"
            cover_path.write_bytes(cover_bytes)
            _safe_print(f"[Cover] Đã tải xong cover: {cover_path}")

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


def _selected_chapter_data(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: Path,
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
    book_dir: Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
    cover_bytes: Optional[bytes] = None,
    cover_ext: Optional[str] = None,
) -> Path:
    import epub_builder

    start, end = _normalize_range(len(chapters), start, end)
    selected_chapters = chapters[start - 1:end]
    chapters_data = _selected_chapter_data(book_info, chapters, book_dir, start, end)

    suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    epub_path = OUTPUT_BASE / f"{_safe_filename(book_info['title'])}{suffix}.epub"

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
            html_cache_dir=str(book_dir),
            chapters_data=chapters_data,
            language="zh-CN",
        )
    _safe_print(f"[Epub] Đã tạo xong ebook: {epub_path}")
    return epub_path


def _ask_int(prompt: str, default: Optional[int] = None) -> int:
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        try:
            return int(raw)
        except ValueError:
            _safe_print("Vui lòng nhập số hợp lệ.")


def main() -> None:
    _safe_print("Downloader bxwx9.org / 笔下文学网")
    raw_url = input(f"Nhập url [{DEFAULT_URL}]: ").strip()
    book_info, chapters, book_dir, cover_bytes, cover_ext = _prepare_book_context(raw_url or DEFAULT_URL)

    while True:
        _safe_print("\n-----------------Menu-----------------")
        _safe_print("[1] Tải toàn bộ (HTML + EPUB) - Default")
        _safe_print("[2] Tải toàn bộ (HTML/TXT)")
        _safe_print("[3] Tải từ chương X đến chương Y (HTML + EPUB)")
        _safe_print("[4] Tạo EPUB từ cache hiện có")
        _safe_print("[5] Nhập URL truyện mới")
        _safe_print("[0] Thoát")
        choice = input("Chọn [1]: ").strip() or "1"

        try:
            if choice == "1":
                save_all_chapters_to_html(book_info["title"], chapters, str(book_dir))
                build_epub(book_info, chapters, book_dir, cover_bytes=cover_bytes, cover_ext=cover_ext)
            elif choice == "2":
                save_all_chapters_to_html(book_info["title"], chapters, str(book_dir))
                save_txt_from_html(book_info, chapters, str(book_dir))
            elif choice == "3":
                start = _ask_int("Chương bắt đầu: ")
                end = _ask_int("Chương kết thúc: ", len(chapters))
                save_all_chapters_to_html(book_info["title"], chapters, str(book_dir), start=start, end=end)
                build_epub(book_info, chapters, book_dir, start=start, end=end, cover_bytes=cover_bytes, cover_ext=cover_ext)
            elif choice == "4":
                build_epub(book_info, chapters, book_dir, cover_bytes=cover_bytes, cover_ext=cover_ext)
            elif choice == "5":
                raw_url = input("Nhập url mới: ").strip()
                if not raw_url:
                    _safe_print("URL trống, giữ nguyên truyện hiện tại.")
                    continue
                book_info, chapters, book_dir, cover_bytes, cover_ext = _prepare_book_context(raw_url)
            elif choice == "0":
                break
            else:
                _safe_print("Lựa chọn không hợp lệ.")
        except Exception as exc:
            _safe_print(f"Lỗi: {exc}")


if __name__ == "__main__":
    main()
