# -*- coding: utf-8 -*-
"""
Downloader cho https://www.piaotia.com/.

Chức năng:
- Lấy thông tin truyện, cover và danh sách chương.
- Tải từng chương ra HTML ngay khi tải xong để có thể chạy tiếp.
- Xử lý cover bằng Pillow và đóng gói EPUB thủ công bằng zipfile.
"""

from __future__ import annotations

import html
import importlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from uuid import NAMESPACE_URL, uuid5
from download_logger import chapter_log_line


def _safe_print(message: str = "") -> None:
    try:
        sys.stdout.buffer.write((message + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        print(message, flush=True)


def _import_or_install(import_name: str, pip_name: Optional[str] = None):
    try:
        return importlib.import_module(import_name)
    except ImportError:
        package = pip_name or import_name
        _safe_print(f"[Setup] Thiếu thư viện {package}. Đang tự cài bằng pip...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        return importlib.import_module(import_name)


requests = _import_or_install("requests")
bs4_module = _import_or_install("bs4", "beautifulsoup4")
_import_or_install("PIL", "Pillow")

from bs4 import BeautifulSoup  # noqa: E402
from PIL import Image  # noqa: E402


BASE_URL = "https://www.piaotia.com/"
DEFAULT_URL = "https://www.piaotia.com/bookinfo/10/10902.html"
OUTPUT_BASE = Path("output")
PUBLISHER = "StoryDownloader"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,vi;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": BASE_URL,
}

TIMEOUT = 25
SLEEP_BETWEEN_CHAPS = 0.75
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)
MIN_COVER_HEIGHT = 1200

SESSION = requests.Session()


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


def _book_ids_from_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    path = urlparse(url).path
    match = re.search(r"/(?:bookinfo|html)/(\d+)/(\d+)(?:/|\.html|$)", path, flags=re.I)
    if match:
        return match.group(1), match.group(2)
    return None, None


def _info_url_from_ids(category_id: str, book_id: str) -> str:
    return f"{BASE_URL}bookinfo/{category_id}/{book_id}.html"


def _catalog_url_from_ids(category_id: str, book_id: str) -> str:
    return f"{BASE_URL}html/{category_id}/{book_id}/index.html"


def _normalize_catalog_url(url: str) -> str:
    parsed = urlparse(url)
    if re.search(r"/html/\d+/\d+/?$", parsed.path, flags=re.I):
        return url.rstrip("/") + "/index.html"
    return url


def _is_info_url(url: str) -> bool:
    return re.search(r"/bookinfo/\d+/\d+\.html?$", urlparse(url).path, flags=re.I) is not None


def _is_catalog_url(url: str) -> bool:
    path = urlparse(url).path
    return re.search(r"/html/\d+/\d+/(?:index\.html?)?$", path, flags=re.I) is not None


def _is_chapter_url(url: str) -> bool:
    return re.search(r"/html/\d+/\d+/\d+\.html?$", urlparse(url).path, flags=re.I) is not None


def _http_get(url: str, referer: Optional[str] = None, tries: int = 3, backoff: float = 0.8):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    last_exc = None
    for attempt in range(1, tries + 1):
        try:
            response = SESSION.get(_ensure_url(url), headers=headers, timeout=TIMEOUT)
            if response.status_code in RETRY_STATUS and attempt < tries:
                _safe_print(f"[HTTP] {response.status_code} khi tải {url}. Thử lại {attempt}/{tries}...")
                time.sleep(backoff * attempt)
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < tries:
                _safe_print(f"[HTTP] Lỗi khi tải {url}: {exc}. Thử lại {attempt}/{tries}...")
                time.sleep(backoff * attempt)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Không tải được URL: {url}")


def _detect_encoding(content: bytes, response=None) -> str:
    candidates: List[str] = []

    content_type = ""
    if response is not None:
        content_type = getattr(response, "headers", {}).get("content-type", "") or ""
    match = re.search(r"charset=([\w\-]+)", content_type, flags=re.I)
    if match:
        candidates.append(match.group(1))

    head = content[:8192].decode("ascii", errors="ignore")
    match = re.search(r"charset=['\"]?([\w\-]+)", head, flags=re.I)
    if match:
        candidates.append(match.group(1))

    encoding = getattr(response, "encoding", None) if response is not None else None
    apparent = getattr(response, "apparent_encoding", None) if response is not None else None
    if encoding:
        candidates.append(encoding)
    if apparent:
        candidates.append(apparent)

    candidates.extend(["utf-8", "gb18030", "gbk", "gb2312", "big5"])
    seen = set()
    for encoding in candidates:
        if not encoding:
            continue
        normalized = encoding.lower().replace("_", "-")
        if normalized in seen or normalized in {"iso-8859-1", "latin-1", "ascii"}:
            continue
        seen.add(normalized)
        try:
            content.decode(encoding)
            return encoding
        except Exception:
            continue
    return "utf-8"


def _decode_html(content: bytes, response=None) -> str:
    return content.decode(_detect_encoding(content, response), errors="replace")


def _fetch_page(url: str, referer: Optional[str] = None) -> Tuple[BeautifulSoup, str, int]:
    response = _http_get(url, referer=referer)
    text = _decode_html(response.content, response)
    return BeautifulSoup(text, "html.parser"), text, response.status_code


def _first_meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_spaces(tag["content"])
    return ""


def _clean_book_title(value: str) -> str:
    value = _clean_spaces(value)
    value = re.sub(r"最新章节.*$", "", value)
    value = re.sub(r"无弹窗.*$", "", value)
    value = re.sub(r"[_,-]?飘天文学.*$", "", value)
    value = re.sub(r"[_,-]?PT文学.*$", "", value)
    return _clean_spaces(value) or "Unknown"


def _book_title_from_soup(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        title = _clean_book_title(_text(h1))
        if title:
            return title

    title_text = _text(soup.find("title"))
    if title_text:
        if "," in title_text:
            title_text = title_text.split(",", 1)[0]
        return _clean_book_title(title_text)
    return "Unknown"


def _regex_from_text(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.I)
    return _clean_spaces(match.group(1)) if match else ""


def _parse_intro_from_info(raw_html: str, soup: BeautifulSoup) -> str:
    cleaned_html = re.sub(r"<script\b.*?</script>", "", raw_html, flags=re.I | re.S)
    cleaned_html = re.sub(r"<style\b.*?</style>", "", cleaned_html, flags=re.I | re.S)

    patterns = [
        r"<span[^>]*>\s*内容简介[：:]\s*</span>\s*<br\s*/?>(?P<body>.*?)(?:</td>|<table\b[^>]*class=[\"']grid)",
        r"内容简介[：:]\s*</span>\s*<br\s*/?>(?P<body>.*?)(?:</td>|<table\b[^>]*class=[\"']grid)",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned_html, flags=re.I | re.S)
        if match:
            fragment = match.group("body")
            fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
            text = BeautifulSoup(fragment, "html.parser").get_text("\n", strip=True)
            lines = [_clean_spaces(line) for line in text.splitlines()]
            intro = "\n".join(line for line in lines if line)
            if intro:
                return intro

    text = soup.get_text("\n", strip=True)
    start = text.find("内容简介")
    if start >= 0:
        tail = text[start:].split("最新章节预览", 1)[0]
        tail = re.sub(r"^内容简介[：:]\s*", "", tail)
        lines = [_clean_spaces(line) for line in tail.splitlines()]
        return "\n".join(line for line in lines if line)
    return ""


def _find_bookinfo_url(soup: BeautifulSoup, page_url: str) -> Optional[str]:
    for a in soup.find_all("a", href=True):
        href = _absolute_url(page_url, a["href"])
        if _is_info_url(href):
            return href
    return None


def _find_catalog_url(soup: BeautifulSoup, page_url: str) -> Optional[str]:
    category_id, book_id = _book_ids_from_url(page_url)
    if category_id and book_id:
        return _catalog_url_from_ids(category_id, book_id)

    candidates: List[str] = []
    for a in soup.find_all("a", href=True):
        label = _clean_spaces(a.get_text(" ", strip=True))
        href = _absolute_url(page_url, a["href"])
        path = urlparse(href).path
        if "章节目录" in label or "查看全部章节" in label or re.search(r"/html/\d+/\d+/(?:index\.html?)?$", path):
            candidates.append(_normalize_catalog_url(href))
    return candidates[0] if candidates else None


def _parse_cover_url(soup: BeautifulSoup, page_url: str, category_id: Optional[str], book_id: Optional[str]) -> str:
    expected = f"/files/article/image/{category_id}/{book_id}/" if category_id and book_id else ""
    images: List[str] = []
    for img in soup.find_all("img", src=True):
        src = _absolute_url(page_url, img["src"])
        if expected and expected in urlparse(src).path:
            return src
        if "/files/article/image/" in urlparse(src).path:
            images.append(src)
    return images[0] if images else ""


def _get_book_info(info_soup: BeautifulSoup, info_html: str, page_url: str, catalog_soup: Optional[BeautifulSoup] = None) -> Dict[str, str]:
    category_id, book_id = _book_ids_from_url(page_url)
    source_soup = info_soup or catalog_soup
    full_text = source_soup.get_text("\n", strip=True) if source_soup else ""

    title = _book_title_from_soup(info_soup)
    if title == "Unknown" and catalog_soup:
        title = _book_title_from_soup(catalog_soup)

    author = _regex_from_text(full_text, r"作\s*者[：:]\s*([^\n\r ]+)")
    if not author and catalog_soup:
        author = _first_meta(catalog_soup, "author")
    if not author:
        author = "Unknown"

    category = _regex_from_text(full_text, r"类\s*别[：:]\s*([^\n\r ]+)")
    status = _regex_from_text(full_text, r"文章状态[：:]\s*([^\n\r ]+)")
    update_time = _regex_from_text(full_text, r"最后更新[：:]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")
    intro = _parse_intro_from_info(info_html, info_soup) if info_html else ""

    latest_chapter = ""
    latest_chapter_url = ""
    for marker in info_soup.find_all(string=re.compile("最新章节")):
        parent = marker.parent
        link = parent.find_next("a", href=True) if parent else None
        if link and _is_chapter_url(_absolute_url(page_url, link["href"])):
            latest_chapter = _clean_spaces(link.get_text(" ", strip=True))
            latest_chapter_url = _absolute_url(page_url, link["href"])
            break

    cover_url = _parse_cover_url(info_soup, page_url, category_id, book_id)

    return {
        "title": title,
        "author": author,
        "category": category,
        "status": status,
        "update_time": update_time,
        "latest_chapter": latest_chapter,
        "latest_chapter_url": latest_chapter_url,
        "intro": intro,
        "cover_url": cover_url,
        "url": page_url,
    }


def _get_list_chapters(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    container = soup.select_one(".centent") or soup.select_one(".mainbody") or soup
    category_id, book_id = _book_ids_from_url(page_url)
    expected_prefix = f"/html/{category_id}/{book_id}/" if category_id and book_id else ""

    chapters: List[Dict[str, str]] = []
    seen = set()
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.lower().startswith(("javascript:", "#")):
            continue
        url = _absolute_url(page_url, href)
        path = urlparse(url).path
        basename = os.path.basename(path).lower()
        if basename in {"", "index.html", "index.htm"}:
            continue
        if not re.fullmatch(r"\d+\.html?", basename, flags=re.I):
            continue
        if expected_prefix and expected_prefix not in path:
            continue
        title = _clean_spaces(a.get_text(" ", strip=True))
        if not title or url in seen:
            continue
        seen.add(url)
        chapters.append({"title": title, "url": url})
    return chapters


def getText(url: str = DEFAULT_URL) -> Dict[str, object]:
    url = _ensure_url(url)
    _safe_print(f"[Info] Đang đọc URL: {url}")
    source_soup, source_html, _ = _fetch_page(url)

    category_id, book_id = _book_ids_from_url(url)
    info_url = _find_bookinfo_url(source_soup, url)
    catalog_url = _find_catalog_url(source_soup, url)
    if category_id and book_id:
        info_url = info_url or _info_url_from_ids(category_id, book_id)
        catalog_url = catalog_url or _catalog_url_from_ids(category_id, book_id)

    if not info_url:
        info_url = url if _is_info_url(url) else DEFAULT_URL
    if not catalog_url:
        catalog_url = url if _is_catalog_url(url) else _find_catalog_url(source_soup, info_url or url)
    if not catalog_url:
        raise RuntimeError("Không tìm thấy URL mục lục.")
    catalog_url = _normalize_catalog_url(catalog_url)

    if _is_info_url(url):
        info_soup, info_html = source_soup, source_html
    else:
        _safe_print(f"[Info] Đang tải trang info: {info_url}")
        info_soup, info_html, _ = _fetch_page(info_url, referer=url)

    if _is_catalog_url(url):
        catalog_soup = source_soup
    else:
        _safe_print(f"[Info] Đang tải mục lục: {catalog_url}")
        catalog_soup, _, _ = _fetch_page(catalog_url, referer=info_url)

    book_info = _get_book_info(info_soup, info_html, info_url, catalog_soup)
    chapters = _get_list_chapters(catalog_soup, catalog_url)
    if not chapters:
        raise RuntimeError("Không lấy được danh sách chương từ mục lục.")

    book_info["catalog_url"] = catalog_url
    return {**book_info, "chapters": chapters}


def _clean_chapter_title(raw_title: str, book_title: str = "", fallback: str = "") -> str:
    title = _clean_spaces(raw_title)
    title = re.sub(r"最新章节[,，]?\s*", "", title)
    title = re.sub(r",?\s*PT文学.*$", "", title)
    title = re.sub(r",?\s*飘天文学.*$", "", title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title):]
    title = title.strip(" -:_，,")
    return _clean_spaces(title) or fallback or "Chương"


def _chapter_title_from_page(soup: BeautifulSoup, book_title: str = "", fallback: str = "") -> str:
    h1 = soup.find("h1")
    if h1:
        return _clean_chapter_title(_text(h1), book_title, fallback)

    title_tag = soup.find("title")
    if title_tag:
        title = _text(title_tag)
        match = re.search(r",\s*(.*?)\s*,", title)
        if match:
            return _clean_chapter_title(match.group(1), book_title, fallback)
        return _clean_chapter_title(title, book_title, fallback)
    return fallback or "Chương"


def _is_noise_line(line: str) -> bool:
    if not line:
        return True
    exact = {
        "上一章",
        "下一章",
        "返回目录",
        "返回书页",
        "加入书签",
        "推荐本书",
        "我的书架",
        "加入书架",
        "收藏本书",
        "繁體中文",
    }
    if line in exact:
        return True
    if line.startswith("选择背景颜色") or line.startswith("选择字体大小"):
        return True
    if line.startswith("（快捷键") or line.startswith("章节错误"):
        return True
    if "飘天文学" in line and ("Copyright" in line or "小说阅读网" in line):
        return True
    return False


def _extract_chapter_content_html(raw_html: str) -> str:
    end_positions = []
    for pattern in [
        r"<!--\s*翻页上AD开始",
        r"<div\s+class=[\"']bottomlink[\"']",
        r"<div\s+id=[\"']Commenddiv[\"']",
        r"<div\s+id=[\"']feit2[\"']",
    ]:
        match = re.search(pattern, raw_html, flags=re.I)
        if match:
            end_positions.append(match.start())
    end = min(end_positions) if end_positions else len(raw_html)

    start = 0
    top_link = re.search(r"<div\s+class=[\"']toplink[\"'][^>]*>.*?</div>", raw_html, flags=re.I | re.S)
    if top_link and top_link.end() < end:
        start = top_link.end()
    else:
        h1 = re.search(r"</h1\s*>", raw_html, flags=re.I)
        if h1 and h1.end() < end:
            start = h1.end()

    fragment = raw_html[start:end]
    fragment = re.sub(r"<script\b.*?</script>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style\b.*?</style>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)

    soup = BeautifulSoup(fragment, "html.parser")
    for bad in soup.find_all(["script", "style", "table", "center", "iframe", "form", "noscript"]):
        bad.decompose()

    text = soup.get_text("\n", strip=False)
    paragraphs: List[str] = []
    for raw_line in text.splitlines():
        line = _clean_spaces(raw_line)
        if not line or _is_noise_line(line):
            continue
        paragraphs.append(line)

    if not paragraphs:
        return "<p>(Không có nội dung)</p>"
    return "\n".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)


def fetch_chapter_content(url: str, book_title: str = "", fallback_title: str = "") -> Dict[str, str]:
    try:
        soup, raw_html, status_code = _fetch_page(url, referer=_normalize_catalog_url(url.rsplit("/", 1)[0] + "/"))
        title = _chapter_title_from_page(soup, book_title, fallback_title)
        content_html = _extract_chapter_content_html(raw_html)
        text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
        if len(text) < 20:
            raise RuntimeError("Nội dung chương quá ngắn hoặc rỗng.")
        return {
            "title": title,
            "content_html": content_html,
            "text": text,
            "url": url,
            "status_code": str(status_code),
        }
    except Exception as exc:
        _safe_print(f"[HTML] Không tải được chương {url}: {exc}")
        return {
            "title": fallback_title or "Chương lỗi",
            "content_html": "<p>(Không tải được nội dung)</p>",
            "text": "",
            "url": url,
            "status_code": "ERROR",
        }


def _chapter_html_doc(title: str, content_html: str, source_url: str = "") -> str:
    source = ""
    if source_url:
        source = f'\n<footer><p class="source"><a href="{html.escape(source_url, quote=True)}">{html.escape(source_url)}</a></p></footer>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: serif; line-height: 1.75; margin: 2rem auto; max-width: 760px; padding: 0 1rem; }}
    h1 {{ font-size: 1.45rem; line-height: 1.35; margin: 0 0 1.5rem; text-align: center; }}
    p {{ margin: 0 0 0.75rem; text-indent: 2em; }}
    .source {{ color: #666; font-size: 0.85rem; text-indent: 0; }}
  </style>
</head>
<body>
<article>
  <h1>{html.escape(title)}</h1>
{content_html}
</article>{source}
</body>
</html>
"""


def _chapter_html_path(book_dir: Path, idx: int) -> Path:
    return book_dir / f"chapter_{idx:04d}.html"


def _find_cached_chapter_path(book_dir: Path, idx: int) -> Optional[Path]:
    candidates = [
        _chapter_html_path(book_dir, idx),
        book_dir / "html" / f"{idx:04d}.html",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for pattern in (f"{idx:04d} - *.html", f"{idx:04d}.html"):
        matches = sorted(book_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def _read_cached_chapter(html_path: Path) -> Dict[str, str]:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    title = _text(soup.find("h1")) or _text(soup.find("title")) or html_path.stem
    article = soup.find("article")
    if article:
        fragment = "".join(str(child) for child in article.contents)
    else:
        root = soup.body or soup
        fragment = "".join(str(child) for child in root.contents)
    article_soup = BeautifulSoup(fragment, "html.parser")
    for h1 in article_soup.find_all("h1"):
        h1.decompose()
    content_html = "\n".join(str(child) for child in article_soup.contents).strip() or "<p>(Không có nội dung)</p>"
    text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
    return {"title": title, "content_html": content_html, "text": text, "url": str(html_path), "status_code": "CACHE"}


def _save_progress(book_dir: Path, idx: int, data: Dict[str, str]) -> None:
    progress = {
        "last_saved_index": idx,
        "last_saved_title": data.get("title", ""),
        "last_saved_url": data.get("url", ""),
        "last_saved_at": datetime.now(timezone.utc).isoformat(),
    }
    (book_dir / "progress.json").write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_chapter_html(
    chapter: Dict[str, str],
    idx: int,
    book_dir: Path,
    *,
    book_title: str = "",
    force: bool = False,
) -> Dict[str, str]:
    book_dir.mkdir(parents=True, exist_ok=True)
    html_path = _chapter_html_path(book_dir, idx)

    cached_path = _find_cached_chapter_path(book_dir, idx)
    if cached_path and not force:
        return _read_cached_chapter(cached_path)

    title = chapter.get("title") or f"Chương {idx}"
    url = chapter["url"]
    data = fetch_chapter_content(url, book_title=book_title, fallback_title=title)
    if not data.get("title"):
        data["title"] = title

    html_path.write_text(_chapter_html_doc(data["title"], data["content_html"], data["url"]), encoding="utf-8")
    data["html_path"] = str(html_path)
    _save_progress(book_dir, idx, data)
    return data


def _normalize_range(total: int, start: int = 1, end: Optional[int] = None) -> Tuple[int, int]:
    if total <= 0:
        raise ValueError("Danh sách chương rỗng.")
    end = total if end is None else end
    start = max(1, min(int(start), total))
    end = max(1, min(int(end), total))
    if start > end:
        start, end = end, start
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
    selected_total = end - start + 1
    _safe_print(f"[HTML] Bắt đầu tải/cache {selected_total} chương vào: {book_dir}")
    items: List[Dict[str, str]] = []
    for done, idx in enumerate(range(start, end + 1), 1):
        data = _save_chapter_html(
            chapters[idx - 1],
            idx,
            book_dir,
            book_title=book_info.get("title", ""),
            force=force,
        )
        items.append(data)
        title = data.get("title") or chapters[idx - 1].get("title") or ""
        _safe_print(chapter_log_line(done, selected_total, data.get("status_code", "ERR"), idx, len(chapters), title))
        if idx < end:
            time.sleep(SLEEP_BETWEEN_CHAPS)
    _safe_print(f"[HTML] Hoàn tất tải/cache {len(items)} chương.")
    return items


def save_all_chapters_to_html(
    book_title: str,
    chapters: List[Dict[str, str]],
    out_dir: str,
    start: int = 1,
    end: Optional[int] = None,
    force: bool = False,
) -> List[Dict[str, str]]:
    book_info = {"title": book_title}
    return download_chapters(book_info, chapters, Path(out_dir), start=start, end=end, force=force)


def _cover_resample():
    resampling = getattr(Image, "Resampling", None)
    if resampling and hasattr(resampling, "LANCZOS"):
        return resampling.LANCZOS
    return getattr(Image, "LANCZOS", getattr(Image, "BICUBIC", 3))


def _download_cover(cover_url: str) -> Tuple[Optional[bytes], Optional[str]]:
    if not cover_url or cover_url.startswith("data:"):
        return None, None
    try:
        _safe_print(f"[Cover] Đang tải cover: {cover_url}")
        response = _http_get(cover_url)
        content = response.content
        ext = Path(urlparse(cover_url).path).suffix.lower() or ".jpg"
        if ext == ".jpeg":
            ext = ".jpg"
        return content, ext
    except Exception as exc:
        _safe_print(f"[Cover] Không tải được cover: {exc}")
        return None, None


def _resize_cover(content: bytes, ext: str) -> Tuple[bytes, str]:
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            original_size = image.size
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                background = Image.new("RGB", image.size, "white")
                rgba = image.convert("RGBA")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")

            if image.height < MIN_COVER_HEIGHT:
                scale = MIN_COVER_HEIGHT / max(1, image.height)
                new_size = (min(MAX_COVER_SIZE[0], int(image.width * scale)), min(MAX_COVER_SIZE[1], int(image.height * scale)))
                image = image.resize(new_size, _cover_resample())
            elif image.width > MAX_COVER_SIZE[0] or image.height > MAX_COVER_SIZE[1]:
                image.thumbnail(MAX_COVER_SIZE, _cover_resample())

            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90, optimize=True)
            _safe_print(f"[Cover] Resize/convert cover: {original_size[0]}x{original_size[1]} -> {image.width}x{image.height} JPEG")
            return output.getvalue(), ".jpg"
    except Exception as exc:
        _safe_print(f"[Cover] Không xử lý được cover bằng Pillow: {exc}")
        return content, ext or ".jpg"


def _load_cover(book_info: Dict[str, str], book_dir: Path) -> Tuple[Optional[bytes], Optional[str]]:
    cover_path = book_dir / "cover.jpg"
    if cover_path.is_file():
        _safe_print(f"[Cover] Dùng cover đã có: {cover_path}")
        return cover_path.read_bytes(), ".jpg"

    cover_bytes, cover_ext = _download_cover(book_info.get("cover_url", ""))
    if cover_bytes and cover_ext:
        cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
        cover_path.write_bytes(cover_bytes)
        _safe_print(f"[Cover] Đã lưu cover: {cover_path}")
        return cover_bytes, cover_ext
    return None, None


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


def _zip_write(zipf: zipfile.ZipFile, arcname: str, data, *, compress: bool = True) -> None:
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    if isinstance(data, str):
        data = data.encode("utf-8")
    zipf.writestr(zinfo, data)


def _selected_chapter_data(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: Path,
    start: int,
    end: int,
) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for idx in range(start, end + 1):
        cached = _find_cached_chapter_path(book_dir, idx)
        if cached:
            items.append(_read_cached_chapter(cached))
        else:
            items.append(
                _save_chapter_html(
                    chapters[idx - 1],
                    idx,
                    book_dir,
                    book_title=book_info.get("title", ""),
                )
            )
    return items


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
    items = _selected_chapter_data(book_info, chapters, book_dir, start, end)

    if not cover_bytes:
        cover_path = book_dir / "cover.jpg"
        if cover_path.is_file():
            cover_bytes, cover_ext = cover_path.read_bytes(), ".jpg"

    title = book_info.get("title", "Truyện")
    author = book_info.get("author", "Unknown")
    intro = book_info.get("intro", "")
    uid = f"urn:uuid:{uuid5(NAMESPACE_URL, book_info.get('url', title))}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    epub_path = book_dir / f"{_safe_filename(title)}{suffix}.epub"

    has_cover = bool(cover_bytes)
    cover_name = "Images/cover.jpg"

    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="style" href="Styles/style.css" media-type="text/css"/>',
        '<item id="titlepage" href="Text/title.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_items = ['<itemref idref="titlepage"/>']
    nav_links = ['<li><a href="Text/title.xhtml">Thông tin truyện</a></li>']

    if has_cover:
        manifest_items.append(f'<item id="cover-image" href="{cover_name}" media-type="image/jpeg" properties="cover-image"/>')
        manifest_items.append('<item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
        spine_items.insert(0, '<itemref idref="cover"/>')
        nav_links.insert(0, '<li><a href="Text/cover.xhtml">Cover</a></li>')

    ncx_points: List[str] = []
    play_order = 1
    if has_cover:
        ncx_points.append(
            f'<navPoint id="cover" playOrder="{play_order}"><navLabel><text>Cover</text></navLabel><content src="Text/cover.xhtml"/></navPoint>'
        )
        play_order += 1
    ncx_points.append(
        f'<navPoint id="titlepage" playOrder="{play_order}"><navLabel><text>Thông tin truyện</text></navLabel><content src="Text/title.xhtml"/></navPoint>'
    )
    play_order += 1

    for order, chapter in enumerate(items, 1):
        file_name = f"Text/chapter_{order:04d}.xhtml"
        manifest_items.append(f'<item id="chap{order}" href="{file_name}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="chap{order}"/>')
        chapter_title = chapter.get("title") or f"Chương {start + order - 1}"
        nav_links.append(f'<li><a href="{file_name}">{html.escape(chapter_title)}</a></li>')
        ncx_points.append(
            f'<navPoint id="chap{order}" playOrder="{play_order}">'
            f"<navLabel><text>{html.escape(chapter_title)}</text></navLabel>"
            f'<content src="{file_name}"/></navPoint>'
        )
        play_order += 1

    style_css = """
body { font-family: serif; line-height: 1.75; margin: 5%; }
h1 { font-size: 1.35em; line-height: 1.35; margin: 0 0 1.2em; text-align: center; }
p { margin: 0 0 0.75em; text-indent: 2em; }
.meta, .intro { text-indent: 0; }
.cover { margin: 0; padding: 0; text-align: center; text-indent: 0; }
.cover img { height: auto; max-height: 98vh; max-width: 100%; }
nav ol { padding-left: 1.4em; }
"""

    title_body = [
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="meta"><strong>Tác giả:</strong> {html.escape(author)}</p>',
    ]
    if book_info.get("category"):
        title_body.append(f'<p class="meta"><strong>Thể loại:</strong> {html.escape(book_info["category"])}</p>')
    if book_info.get("status"):
        title_body.append(f'<p class="meta"><strong>Trạng thái:</strong> {html.escape(book_info["status"])}</p>')
    if book_info.get("url"):
        title_body.append(f'<p class="meta"><strong>Nguồn:</strong> {html.escape(book_info["url"])}</p>')
    if intro:
        title_body.append("<hr/>")
        for line in intro.splitlines():
            line = _clean_spaces(line)
            if line:
                title_body.append(f'<p class="intro">{html.escape(line)}</p>')
    title_xhtml = _xhtml_page(title, "\n".join(title_body))

    nav_xhtml = _xhtml_page(
        "Mục lục",
        f'<nav epub:type="toc" id="toc"><h1>Mục lục</h1><ol>{"".join(nav_links)}</ol></nav>',
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
  <navMap>
    {''.join(ncx_points)}
  </navMap>
</ncx>
"""

    content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="BookId">{html.escape(uid)}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>{html.escape(author)}</dc:creator>
    <dc:language>zh-CN</dc:language>
    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>
    <dc:source>{html.escape(book_info.get("url", ""))}</dc:source>
    <meta property="dcterms:modified">{modified}</meta>
    {'<meta name="cover" content="cover-image"/>' if has_cover else ''}
  </metadata>
  <manifest>
    {''.join(manifest_items)}
  </manifest>
  <spine toc="ncx">
    {''.join(spine_items)}
  </spine>
</package>
"""

    container_xml = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    _safe_print(f"[Epub] Đang đóng gói EPUB thủ công: {epub_path}")
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
            chapter_title = chapter.get("title") or f"Chương {start + order - 1}"
            body = f"<h1>{html.escape(chapter_title)}</h1>\n{chapter.get('content_html') or '<p>(Không có nội dung)</p>'}"
            _zip_write(zf, f"OEBPS/Text/chapter_{order:04d}.xhtml", _xhtml_page(chapter_title, body))

    _safe_print(f"[Epub] Đã tạo EPUB: {epub_path}")
    return epub_path


def _prepare_book_dir(book_info: Dict[str, str]) -> Path:
    book_dir = OUTPUT_BASE / _safe_filename(book_info.get("title") or "book")
    book_dir.mkdir(parents=True, exist_ok=True)
    return book_dir


def _save_book_info(book_info: Dict[str, str], chapters: List[Dict[str, str]], book_dir: Path) -> None:
    lines = [
        f"Title: {book_info.get('title', '')}",
        f"Author: {book_info.get('author', '')}",
        f"Category: {book_info.get('category', '')}",
        f"Status: {book_info.get('status', '')}",
        f"Update time: {book_info.get('update_time', '')}",
        f"URL: {book_info.get('url', '')}",
        f"Catalog: {book_info.get('catalog_url', '')}",
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
    (book_dir / "book_info.json").write_text(json.dumps(book_info, ensure_ascii=False, indent=2), encoding="utf-8")
    (book_dir / "chapters.json").write_text(json.dumps(chapters, ensure_ascii=False, indent=2), encoding="utf-8")
    _safe_print(f"[Info] Đã lưu metadata: {book_dir / 'book_info.txt'}")


def _load_book_context(url: str) -> Tuple[Dict[str, str], List[Dict[str, str]], Path, Optional[bytes], Optional[str]]:
    url = _ensure_url(url)
    _safe_print("[Info] Đang lấy thông tin truyện...")
    data = getText(url)
    book_info = {
        "title": str(data.get("title") or "Unknown"),
        "author": str(data.get("author") or "Unknown"),
        "category": str(data.get("category") or ""),
        "status": str(data.get("status") or ""),
        "update_time": str(data.get("update_time") or ""),
        "latest_chapter": str(data.get("latest_chapter") or ""),
        "latest_chapter_url": str(data.get("latest_chapter_url") or ""),
        "intro": str(data.get("intro") or ""),
        "cover_url": str(data.get("cover_url") or ""),
        "url": str(data.get("url") or url),
        "catalog_url": str(data.get("catalog_url") or ""),
    }
    chapters = list(data.get("chapters") or [])
    book_dir = _prepare_book_dir(book_info)
    _save_book_info(book_info, chapters, book_dir)

    _safe_print("\n-----------------Thông tin truyện-----------------")
    _safe_print(f"Tên truyện     : {book_info['title']}")
    _safe_print(f"Tác giả        : {book_info['author']}")
    if book_info.get("category"):
        _safe_print(f"Thể loại       : {book_info['category']}")
    if book_info.get("status"):
        _safe_print(f"Trạng thái     : {book_info['status']}")
    if book_info.get("update_time"):
        _safe_print(f"Cập nhật       : {book_info['update_time']}")
    if book_info.get("latest_chapter"):
        _safe_print(f"Mới nhất       : {book_info['latest_chapter']}")
    _safe_print(f"Số chương      : {len(chapters)}")
    _safe_print(f"Info URL       : {book_info.get('url', '')}")
    _safe_print(f"Mục lục URL    : {book_info.get('catalog_url', '')}")
    _safe_print(f"Thư mục lưu    : {book_dir}")
    _safe_print(f"Mẫu HTML       : {book_dir / 'chapter_0001.html'}")
    _safe_print(f"EPUB sẽ lưu    : {book_dir / (_safe_filename(book_info['title']) + '.epub')}")
    if book_info.get("cover_url"):
        _safe_print(f"Cover URL      : {book_info['cover_url']}")
    if book_info.get("intro"):
        intro = book_info["intro"]
        _safe_print(f"Giới thiệu     : {intro[:160]}{'...' if len(intro) > 160 else ''}")

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
    _safe_print("[1] Tải tất cả ( html + epub ) ( Mặc định )")
    _safe_print("[2] Tải từ chương X tới chương Y")
    _safe_print("[3] Tải chương X")
    _safe_print("[4] Thoát.")


def _post_task_menu() -> bool:
    _safe_print("\n-----------------Menu-----------------")
    _safe_print("[1] Nhập Url mới")
    _safe_print("[2] Thoát ( Mặc định )")
    choice = input("Chọn [2]: ").strip() or "2"
    return choice == "1"


def main() -> None:
    _safe_print("Downloader piaotia.com / 飘天文学")
    while True:
        raw_url = input(f"Nhập URL [{DEFAULT_URL}]: ").strip() or DEFAULT_URL
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
                    _safe_print("\n===== Bắt đầu: Tải toàn bộ HTML + tạo EPUB =====")
                    download_chapters(book_info, chapters, book_dir)
                    epub_path = build_epub_manual(book_info, chapters, book_dir, cover_bytes=cover_bytes, cover_ext=cover_ext)
                    _safe_print(f"===== Hoàn tất. EPUB: {epub_path} =====")
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
