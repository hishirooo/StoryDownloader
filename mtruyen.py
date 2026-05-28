# -*- coding: utf-8 -*-
"""
Downloader cho https://mtruyen.net/

Trang info mẫu:
  Danh Sách Đường Cái Cầu Sinh_ Ta Tại Tận Thế Thăng Cấp Vật Tư (Trọn Bộ) - Sơn Hải Hô Khiếu _ MTruyen.html

Trang chương mẫu:
  Chương 1 - Danh Sách Đường Cái Cầu Sinh_ Ta Tại Tận Thế Thăng Cấp Vật Tư (Trọn Bộ) _ MTruyen.html

Ghi chú:
  Trang info có thể chỉ render một phần mục lục. File này ưu tiên tổng chương từ
  JSON-LD / "Đọc mới nhất" / select trong trang chương rồi tự sinh URL /chuong-N.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from download_logger import chapter_log_line

try:
    import bs4
    import cloudscraper
    import requests
except ImportError:
    os.system(f'"{sys.executable}" -m pip install requests beautifulsoup4 lxml cloudscraper pillow')
    import bs4
    import requests
    try:
        import cloudscraper
    except ImportError:
        cloudscraper = None

from bs4 import BeautifulSoup

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    os.system(f'"{sys.executable}" -m pip install pillow')
    try:
        from PIL import Image
        HAS_PILLOW = True
    except ImportError:
        HAS_PILLOW = False

from epub_builder import create_epub


BASE_URL = "https://mtruyen.net/"
DEFAULT_URL = "https://mtruyen.net/truyen/danh-sach-duong-cai-cau-sinh-ta-tai-tan-the-thang-cap-vat-tu-tron-bo"
OUTPUT_BASE = Path("Output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    "Referer": BASE_URL,
}

TIMEOUT = 30
SLEEP_BETWEEN_CHAPS = 0.8
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)

_SCRAPER = None


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
    value = value.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", value).strip()


def _safe_filename(name: str, max_length: int = 150) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', " - ", name or "book")
    safe = re.sub(r"\s+", " ", safe).strip().strip(".")
    return (safe[:max_length].strip() or "book")


def slugify_vi(value: str) -> str:
    value = _clean_spaces(value).lower()
    value = re.sub(
        r"[àáạảãâầấậẩẫăằắặẳẵ]",
        "a",
        value,
    )
    value = re.sub(r"[èéẹẻẽêềếệểễ]", "e", value)
    value = re.sub(r"[ìíịỉĩ]", "i", value)
    value = re.sub(r"[òóọỏõôồốộổỗơờớợởỡ]", "o", value)
    value = re.sub(r"[ùúụủũưừứựửữ]", "u", value)
    value = re.sub(r"[ỳýỵỷỹ]", "y", value)
    value = value.replace("đ", "d")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "truyen"


def _ensure_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return DEFAULT_URL
    if _resolve_local_path(url):
        return url
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url.lstrip("/")
    return url


def _resolve_local_path(source: str | Path) -> Optional[Path]:
    if not source:
        return None
    path = Path(str(source).strip().strip('"'))
    candidates = [path, Path("output") / path]
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _absolute_url(page_url: str, href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return f"{urlparse(page_url).scheme or 'https'}:{href}"
    return urljoin(page_url or BASE_URL, href)


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_spaces(tag["content"])
    return ""


def _get_scraper():
    global _SCRAPER
    if _SCRAPER is not None:
        return _SCRAPER
    if cloudscraper is not None:
        _SCRAPER = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    else:
        _SCRAPER = requests.Session()
    _SCRAPER.headers.update(HEADERS)
    return _SCRAPER


def _http_get(url: str, referer: Optional[str] = None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    return _get_scraper().get(url, headers=headers, timeout=TIMEOUT)


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
    apparent = getattr(response, "apparent_encoding", None) if response is not None else None
    if apparent:
        candidates.append(apparent)
    candidates.extend(["utf-8", "utf-8-sig"])
    for encoding in candidates:
        if not encoding:
            continue
        if encoding.lower().replace("_", "-") in {"iso-8859-1", "latin-1", "ascii"}:
            continue
        try:
            content.decode(encoding)
            return encoding
        except Exception:
            continue
    return "utf-8"


def _decode_html(content: bytes, response=None) -> str:
    return content.decode(_detect_encoding(content, response), errors="replace")


def _make_soup(text: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(text, "lxml")
    except Exception:
        return BeautifulSoup(text, "html.parser")


def _read_local_html(path: str | Path) -> BeautifulSoup:
    return _make_soup(_decode_html(Path(path).read_bytes()))


def _fetch_html(url: str, tries: int = 3, backoff: float = 0.8) -> BeautifulSoup:
    last_error: Optional[Exception] = None
    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(backoff * attempt)
        try:
            response = _http_get(url)
            status_code = getattr(response, "status_code", 200)
            if status_code in RETRY_STATUS and attempt < tries:
                continue
            response.raise_for_status()
            content = getattr(response, "content", b"") or getattr(response, "text", "").encode("utf-8", errors="replace")
            return _make_soup(_decode_html(content, response))
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Không tải được HTML: {url} ({last_error})")


def _soup_from_source(source: str) -> Tuple[BeautifulSoup, str, Optional[Path]]:
    local_path = _resolve_local_path(source)
    if local_path:
        soup = _read_local_html(local_path)
        canonical = _meta_content(soup, "og:url") or ""
        link = soup.find("link", rel=lambda value: value and "canonical" in value)
        if link and link.get("href"):
            canonical = link["href"]
        return soup, canonical or str(local_path), local_path
    url = _ensure_url(source)
    return _fetch_html(url), url, None


def _json_ld_objects(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    objects: List[Dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            for item in data["@graph"]:
                if isinstance(item, dict):
                    objects.append(item)
        elif isinstance(data, dict):
            objects.append(data)
        elif isinstance(data, list):
            objects.extend(item for item in data if isinstance(item, dict))
    return objects


def _type_contains(item: Dict[str, Any], value: str) -> bool:
    raw_type = item.get("@type")
    if isinstance(raw_type, list):
        return any(str(t).lower() == value.lower() for t in raw_type)
    return str(raw_type or "").lower() == value.lower()


def _schema_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "url", "content", "@id"):
            if value.get(key):
                return _clean_spaces(str(value[key]))
        return ""
    if isinstance(value, list):
        return ", ".join(_schema_text(item) for item in value if _schema_text(item))
    return _clean_spaces(str(value or ""))


def _schema_image(value: Any) -> str:
    if isinstance(value, dict):
        return _schema_text(value.get("url") or value.get("contentUrl"))
    if isinstance(value, list):
        for item in value:
            image = _schema_image(item)
            if image:
                return image
    return _schema_text(value)


def _find_schema_book(soup: BeautifulSoup) -> Dict[str, Any]:
    for item in _json_ld_objects(soup):
        if _type_contains(item, "Book"):
            return item
    for item in _json_ld_objects(soup):
        part = item.get("isPartOf")
        if isinstance(part, dict) and _type_contains(part, "Book"):
            return part
    return {}


def _find_schema_chapter(soup: BeautifulSoup) -> Dict[str, Any]:
    for item in _json_ld_objects(soup):
        if _type_contains(item, "Chapter") or _type_contains(item, "Article"):
            if item.get("position") or item.get("pagination") or item.get("isPartOf"):
                return item
    return {}


def _story_base_from_url(url: str) -> str:
    url = _absolute_url(BASE_URL, url)
    url = re.sub(r"([?#].*)$", "", url).rstrip("/")
    url = re.sub(r"/chuong-\d+/?$", "", url, flags=re.I)
    return url


def _chapter_number_from_url(url: str) -> Optional[int]:
    match = re.search(r"/chuong-(\d+)(?:/)?(?:[?#].*)?$", url or "", flags=re.I)
    return int(match.group(1)) if match else None


def _chapter_url(base_url: str, number: int) -> str:
    return f"{_story_base_from_url(base_url)}/chuong-{number}"


def _clean_chapter_title(text: str, number: Optional[int] = None) -> str:
    title = _clean_spaces(text)
    title = re.sub(r"\s+\d+\s+(phút|giờ|ngày|tuần|tháng|năm)\s+trước$", "", title, flags=re.I)
    if number is not None:
        title = re.sub(rf"^(Chương\s+{number})\s+\d+\s+(phút|giờ|ngày|tuần|tháng|năm)\s+trước$", rf"\1", title, flags=re.I)
    return title or (f"Chương {number}" if number else "Chương")


def _declared_chapters(soup: BeautifulSoup, book_schema: Dict[str, Any]) -> int:
    candidates: List[int] = []
    for key in ("numberOfPages", "numberOfChapters", "totalChapters"):
        try:
            value = int(book_schema.get(key) or 0)
            if value:
                candidates.append(value)
        except Exception:
            pass

    text_sources = [
        _meta_content(soup, "description", "og:description", "twitter:description"),
        _text(soup.select_one("main")),
    ]
    for text in text_sources:
        for match in re.finditer(r"(\d{1,5})\s*chương", text or "", flags=re.I):
            candidates.append(int(match.group(1)))

    for a in soup.select('a[href*="/chuong-"]'):
        number = _chapter_number_from_url(a.get("href", ""))
        if number:
            candidates.append(number)

    for option in soup.select("option[value]"):
        raw = option.get("value", "")
        if raw.isdigit():
            candidates.append(int(raw))

    return max(candidates) if candidates else 0


def _extract_chapters(soup: BeautifulSoup, story_url: str, declared_total: int) -> List[Dict[str, str]]:
    by_number: Dict[int, Dict[str, str]] = {}
    for a in soup.select('a[href*="/chuong-"]'):
        href = _absolute_url(story_url, a.get("href", ""))
        number = _chapter_number_from_url(href)
        if not number:
            continue
        raw_title = _clean_spaces(a.get_text(" ", strip=True))
        if raw_title.lower() in {"đọc từ đầu", "đọc mới nhất", "chương trước", "chương sau"}:
            continue
        by_number[number] = {
            "title": _clean_chapter_title(raw_title, number),
            "url": _chapter_url(story_url, number),
        }

    for option in soup.select("option[value]"):
        raw = option.get("value", "")
        if not raw.isdigit():
            continue
        number = int(raw)
        by_number.setdefault(
            number,
            {"title": _clean_chapter_title(_text(option), number), "url": _chapter_url(story_url, number)},
        )

    total = max(declared_total, max(by_number.keys(), default=0))
    if total:
        for number in range(1, total + 1):
            by_number.setdefault(number, {"title": f"Chương {number}", "url": _chapter_url(story_url, number)})

    return [by_number[number] for number in sorted(by_number)]


def _extract_book_info(source: str) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    soup, page_url, local_path = _soup_from_source(source)
    book_schema = _find_schema_book(soup)
    chapter_schema = _find_schema_chapter(soup)

    schema_url = _schema_text(book_schema.get("url"))
    if not schema_url and isinstance(chapter_schema.get("isPartOf"), dict):
        schema_url = _schema_text(chapter_schema["isPartOf"].get("url"))
    story_url = _story_base_from_url(schema_url or page_url)

    title = (
        _schema_text(book_schema.get("name"))
        or _schema_text((chapter_schema.get("isPartOf") or {}).get("name") if isinstance(chapter_schema.get("isPartOf"), dict) else "")
        or _text(soup.find("h1"))
        or _meta_content(soup, "og:title", "twitter:title")
    )
    title = re.sub(r"\s*\|\s*MTruyen\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*-\s*Chương\s+\d+.*$", "", title, flags=re.I)

    author = _schema_text(book_schema.get("author"))
    if not author and isinstance(chapter_schema.get("author"), dict):
        author = _schema_text(chapter_schema["author"])
    if not author:
        match = re.search(r"Tác giả:\s*(.+?)(?:\s+Thể loại:|\s+(?:Hoàn thành|Đang ra)|$)", _text(soup.select_one("main")), flags=re.I)
        author = _clean_spaces(match.group(1)) if match else "Unknown"

    genres_raw = book_schema.get("genre")
    category = _schema_text(genres_raw)
    if not category:
        match = re.search(r"Thể loại:\s*(.+?)(?:\s+(?:Hoàn thành|Đang ra)|\s+\d+\s+chương|$)", _text(soup.select_one("main")), flags=re.I)
        category = _clean_spaces(match.group(1)) if match else ""

    main_text = _text(soup.select_one("main"))
    status = ""
    match = re.search(r"\b(Hoàn thành|Đang ra|Tạm ngưng|Full)\b", main_text, flags=re.I)
    if match:
        status = _clean_spaces(match.group(1))

    local_cover = ""
    if local_path:
        img = soup.find("img")
        if img:
            local_src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
            if local_src and not re.match(r"^https?://", local_src, flags=re.I):
                local_cover = str((local_path.parent / local_src).resolve())

    cover_url = local_cover or _schema_image(book_schema.get("image")) or _schema_image(chapter_schema.get("image"))
    if not cover_url:
        cover_url = _meta_content(soup, "og:image", "twitter:image")
    if cover_url.startswith("http://localhost"):
        cover_url = cover_url.replace("http://localhost:3000", BASE_URL.rstrip("/"))
    if not cover_url:
        img = soup.find("img")
        if img:
            cover_url = img.get("src") or img.get("data-src") or img.get("data-original") or ""
    if cover_url and local_path and not re.match(r"^https?://", cover_url, flags=re.I):
        cover_url = str((local_path.parent / cover_url).resolve())
    elif cover_url:
        cover_url = _absolute_url(story_url, cover_url)

    intro = _schema_text(book_schema.get("description"))
    if not intro:
        intro = _meta_content(soup, "description", "og:description", "twitter:description")
    intro = re.sub(r"^\s*\|\s*", "", intro)

    declared = _declared_chapters(soup, book_schema)
    chapters = _extract_chapters(soup, story_url, declared)

    # Nếu input là trang info bị render thiếu mục lục, trang chương 1 thường có select đủ chương.
    if declared and len(chapters) < declared:
        for number in range(1, declared + 1):
            if not any(_chapter_number_from_url(item["url"]) == number for item in chapters):
                chapters.append({"title": f"Chương {number}", "url": _chapter_url(story_url, number)})
        chapters.sort(key=lambda item: _chapter_number_from_url(item["url"]) or 0)

    info = {
        "title": title or "Unknown",
        "author": author or "Unknown",
        "status": status,
        "category": category,
        "tags": category,
        "intro": intro,
        "cover_url": cover_url,
        "url": story_url,
        "declared_chapters": declared,
        "_source_path": str(local_path) if local_path else "",
    }
    return info, chapters


def getText(url: str) -> Dict[str, Any]:
    book_info, chapters = _extract_book_info(url)
    return {**book_info, "chapters": chapters, "total_chapters": book_info.get("declared_chapters") or len(chapters)}


def _clean_content_node(node) -> str:
    clone = BeautifulSoup(str(node), "html.parser")
    root = clone.select_one(".chapter-content") or clone
    for bad in root.find_all(["script", "style", "button", "select", "nav"]):
        bad.decompose()
    for tag in root.find_all(True):
        tag.attrs = {}
    return "".join(str(child) for child in root.contents).strip() or "<p>(Không có nội dung)</p>"


def fetch_chapter_content(chapter_url: str) -> Dict[str, str]:
    soup, page_url, _local_path = _soup_from_source(chapter_url)
    title = _text(soup.find("h2")) or _meta_content(soup, "og:title", "twitter:title")
    title = re.sub(r"\s*\|\s*MTruyen\s*$", "", title, flags=re.I)
    title = title.split(" - ", 1)[0].strip() if " - " in title and title.lower().startswith("ch") else title

    content_node = soup.select_one(".chapter-content") or soup.select_one("article") or soup.select_one("main")
    if not content_node:
        raise ValueError(f"Không tìm thấy nội dung chương: {chapter_url}")

    content_clone = BeautifulSoup(str(content_node), "html.parser")
    content_root = content_clone.select_one(".chapter-content") or content_clone
    first_p = content_root.find("p")
    first_text = _clean_spaces(_text(first_p))
    if first_p and re.match(r"^Chương\s*\d+", first_text, flags=re.I):
        title = first_text
        first_p.decompose()

    content_html = _clean_content_node(content_root)
    return {"title": title or "Chương", "content_html": content_html, "url": page_url}


def _download_cover(cover_url: str, source_path: str = "") -> Tuple[Optional[bytes], Optional[str]]:
    cover_url = (cover_url or "").strip()
    if not cover_url:
        return None, None

    local_candidate = _resolve_local_path(cover_url)
    if not local_candidate and source_path and not re.match(r"^https?://", cover_url, flags=re.I):
        local_candidate = (Path(source_path).parent / cover_url).resolve()
    if local_candidate and local_candidate.exists():
        return local_candidate.read_bytes(), local_candidate.suffix.lower() or ".jpg"

    try:
        response = _http_get(cover_url)
        response.raise_for_status()
        ext = Path(urlparse(cover_url).path).suffix.lower() or ".jpg"
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            content_type = response.headers.get("content-type", "").lower()
            ext = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"
        return response.content, ext
    except Exception as exc:
        _safe_print(f"[Cover] Không tải được cover: {exc}")
        return None, None


def _resize_cover(content: bytes, ext: str) -> Tuple[bytes, str]:
    if not content or not HAS_PILLOW:
        return content, ext
    try:
        image = Image.open(io.BytesIO(content))
        image.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
        if image.mode in ("RGBA", "LA"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1])
            image = background
        else:
            image = image.convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90, optimize=True)
        return output.getvalue(), ".jpg"
    except Exception as exc:
        _safe_print(f"[Cover] Không xử lý được cover bằng Pillow: {exc}")
        return content, ext


def _load_cover(book_info: Dict[str, str], book_dir: Path) -> Tuple[Optional[bytes], Optional[str]]:
    cover_url = book_info.get("cover_url") or ""
    if not cover_url:
        _safe_print("[Cover] Không tìm thấy cover.")
        return None, None
    _safe_print(f"[Cover] Đang tải cover: {cover_url}")
    cover_bytes, cover_ext = _download_cover(cover_url, book_info.get("_source_path", ""))
    if cover_bytes and cover_ext:
        cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
        cover_path = book_dir / f"cover{cover_ext}"
        cover_path.write_bytes(cover_bytes)
        _safe_print(f"[Cover] Đã lưu cover: {cover_path}")
        return cover_bytes, cover_ext
    return None, None


def _save_book_info(book_info: Dict[str, str], chapters: List[Dict[str, str]], book_dir: Path) -> None:
    lines = [
        f"Title: {book_info.get('title', '')}",
        f"Author: {book_info.get('author', '')}",
        f"Status: {book_info.get('status', '')}",
        f"Category: {book_info.get('category', '')}",
        f"URL: {book_info.get('url', '')}",
        f"Cover: {book_info.get('cover_url', '')}",
        f"Chapters: {len(chapters)}",
    ]
    if book_info.get("declared_chapters"):
        lines.append(f"Declared chapters: {book_info.get('declared_chapters')}")
    lines.extend(["", book_info.get("intro", ""), "", "Mục lục:"])
    for idx, chapter in enumerate(chapters, 1):
        lines.append(f"{idx:04d}. {chapter['title']} - {chapter['url']}")
    (book_dir / "book_info.txt").write_text("\n".join(lines), encoding="utf-8")


def _normalize_range(total: int, start: int = 1, end: Optional[int] = None) -> Tuple[int, int]:
    if total <= 0:
        return 1, 0
    start = max(1, int(start or 1))
    end = total if end is None else int(end)
    end = min(total, max(start, end))
    return start, end


def _find_cached_chapter_path(book_dir: Path, index: int) -> Optional[Path]:
    for pattern in (f"{index:04d} - *.html", f"{index:04d}.html"):
        matches = sorted(book_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def _read_cached_chapter(path: Path) -> Dict[str, str]:
    soup = _read_local_html(path)
    title = _text(soup.find("h1")) or _text(soup.find("title")) or path.stem
    node = soup.select_one("article") or soup.select_one(".chapter") or soup.body or soup
    for h in node.find_all(["h1"]):
        h.decompose()
    return {"title": title, "content_html": str(node), "url": str(path), "status_code": "CACHE", "html_path": str(path)}


def _chapter_html(title: str, content_html: str, source_url: str, book_title: str = "") -> str:
    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <meta name="source" content="{html.escape(source_url)}"/>
</head>
<body>
  <article class="chapter" data-book="{html.escape(book_title)}">
    <h1>{html.escape(title)}</h1>
    {content_html}
  </article>
</body>
</html>
"""


def _save_chapter_html(chapter: Dict[str, str], index: int, book_dir: Path, *, book_title: str = "") -> Dict[str, str]:
    cached = _find_cached_chapter_path(book_dir, index)
    if cached:
        return _read_cached_chapter(cached)

    data = fetch_chapter_content(chapter["url"])
    title = data.get("title") or chapter.get("title") or f"Chương {index}"
    file_path = book_dir / f"{index:04d} - {_safe_filename(title, 90)}.html"
    file_path.write_text(_chapter_html(title, data.get("content_html", ""), chapter["url"], book_title), encoding="utf-8")
    data["title"] = title
    data["html_path"] = str(file_path)
    data.setdefault("status_code", 200)
    data["url"] = str(file_path)
    return data


def download_chapters(
    book_info: Dict[str, str],
    chapters: List[Dict[str, str]],
    book_dir: Path,
    *,
    start: int = 1,
    end: Optional[int] = None,
) -> List[Dict[str, str]]:
    start, end = _normalize_range(len(chapters), start, end)
    total_selected = end - start + 1
    items: List[Dict[str, str]] = []
    _safe_print(f"Bắt đầu tải/cache {total_selected} chương vào: {book_dir}")
    for done, idx in enumerate(range(start, end + 1), 1):
        chapter = chapters[idx - 1]
        try:
            data = _save_chapter_html(chapter, idx, book_dir, book_title=book_info.get("title", ""))
            items.append(data)
            _safe_print(chapter_log_line(done, total_selected, data.get("status_code", "ERR"), idx, len(chapters), data.get("title") or chapter.get("title") or ""))
        except Exception as exc:
            title = f"{chapter.get('title') or chapter.get('url')} - {exc}"
            _safe_print(chapter_log_line(done, total_selected, "ERR", idx, len(chapters), title))
        time.sleep(SLEEP_BETWEEN_CHAPS)
    _safe_print(f"Hoàn tất tải/cache {total_selected} chương.")
    return items


def save_all_chapters_to_html(
    book_title: str,
    chapters: List[Dict[str, str]],
    out_dir: str | Path,
    start: int = 1,
    end: Optional[int] = None,
) -> List[Dict[str, str]]:
    book_info = {"title": book_title or "Truyện"}
    book_dir = Path(out_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    return download_chapters(book_info, chapters, book_dir, start=start, end=end)


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
            items.append(_save_chapter_html(chapters[idx - 1], idx, book_dir, book_title=book_info.get("title", "")))
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
    start, end = _normalize_range(len(chapters), start, end)
    selected_chapters = chapters[start - 1:end]
    chapters_data = _selected_chapter_data(book_info, chapters, book_dir, start, end)

    title = book_info.get("title") or "Truyện"
    author = book_info.get("author") or "Unknown"
    range_suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
    epub_path = OUTPUT_BASE / f"{_safe_filename(title)} _ {_safe_filename(author)}{range_suffix}.epub"
    epub_path.parent.mkdir(parents=True, exist_ok=True)

    _safe_print(f"[Epub] Đang tạo EPUB: {epub_path}")
    create_epub(
        book_url=book_info.get("url", ""),
        book_title=title,
        author=author,
        chapters=selected_chapters,
        fetch_fn=fetch_chapter_content,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext or ".jpg",
        language="vi",
        out_epub_path=str(epub_path),
        html_cache_dir=str(book_dir),
        chapters_data=chapters_data,
        tags=book_info.get("category") or book_info.get("tags"),
        book_info=book_info,
    )
    _safe_print(f"[Epub] Đã tạo xong EPUB: {epub_path}")
    return epub_path


def _prepare_book_context(source: str) -> Tuple[Dict[str, str], List[Dict[str, str]], Path, Optional[bytes], Optional[str]]:
    source = _ensure_url(source)
    _safe_print("Đang lấy thông tin truyện...")
    data = getText(source)
    chapters = data.get("chapters", [])
    if not chapters:
        raise ValueError("Không tìm thấy danh sách chương.")

    book_info = {
        "title": data.get("title") or "Unknown",
        "author": data.get("author") or "Unknown",
        "status": data.get("status", ""),
        "category": data.get("category", ""),
        "tags": data.get("tags", ""),
        "intro": data.get("intro", ""),
        "cover_url": data.get("cover_url", ""),
        "url": data.get("url", source),
        "declared_chapters": data.get("declared_chapters", 0),
        "_source_path": data.get("_source_path", ""),
    }

    book_dir = OUTPUT_BASE / _safe_filename(book_info["title"])
    book_dir.mkdir(parents=True, exist_ok=True)
    _save_book_info(book_info, chapters, book_dir)
    epub_preview_path = OUTPUT_BASE / f"{_safe_filename(book_info['title'])} _ {_safe_filename(book_info['author'])}.epub"

    _safe_print("\n-----------------Thông tin truyện-----------------")
    _safe_print(f"Tên truyện     : {book_info['title']}")
    _safe_print(f"Tác giả        : {book_info['author']}")
    if book_info.get("status"):
        _safe_print(f"Trạng thái     : {book_info['status']}")
    if book_info.get("category"):
        _safe_print(f"Thể loại       : {book_info['category']}")
    _safe_print(f"Số chương      : {len(chapters)}")
    if book_info.get("declared_chapters") and int(book_info.get("declared_chapters") or 0) != len(chapters):
        _safe_print(f"Số chương khai báo: {book_info['declared_chapters']}")
    _safe_print(f"Cover          : {book_info.get('cover_url') or 'N/A'}")
    _safe_print(f"Thư mục HTML   : {book_dir}")
    _safe_print(f"EPUB sẽ lưu    : {epub_preview_path}")

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


def fetch_cover_from_book_page(book_page_url: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    book_info, _chapters = _extract_book_info(book_page_url)
    cover_bytes, cover_ext = _download_cover(book_info.get("cover_url", ""), book_info.get("_source_path", ""))
    if cover_bytes and cover_ext:
        cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
        return cover_bytes, cover_ext, book_info.get("cover_url", "")
    return None, None, book_info.get("cover_url", "")


def main() -> None:
    _safe_print("Downloader mtruyen.net / MTruyen")
    while True:
        raw_url = input("Nhập Url : ").strip()
        if not raw_url:
            _safe_print("URL trống, vui lòng nhập lại.")
            continue

        try:
            book_info, chapters, book_dir, cover_bytes, cover_ext = _prepare_book_context(raw_url)
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
    import sys as _sys
    from adapter_cli import dispatch_or_menu as _dispatch_or_menu
    _dispatch_or_menu(_sys.modules[__name__], main, default_url=globals().get("DEFAULT_URL", ""))
