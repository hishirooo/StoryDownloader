# -*- coding: utf-8 -*-
"""
Downloader cho kanunu8.com / 努努书坊.

- Parse trang mục lục: tên truyện, tác giả, giới thiệu, danh sách chương.
- Parse trang chương: tiêu đề và nội dung trong div.neirong.
- Lưu HTML/TXT theo từng chương, có thể chạy tiếp từ cache.
- Tạo EPUB thủ công bằng zipfile, không dùng ebooklib/epublib.
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


BASE_URL = "https://www.kanunu8.com/"
DEFAULT_URL = "https://www.kanunu8.com/101/jz_tkcheng/"
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

TIMEOUT = 20
SLEEP_BETWEEN_PAGES = 1.0
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


def _ensure_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return DEFAULT_URL
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url.lstrip("/")
    return url


def _http_get(url: str):
    kwargs = {"headers": HEADERS, "timeout": TIMEOUT}
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

    for enc in candidates:
        if not enc:
            continue
        normalized = enc.lower().replace("_", "-")
        if normalized in {"iso-8859-1", "latin-1", "ascii"}:
            continue
        try:
            content.decode(enc)
            return enc
        except Exception:
            continue
    return "gb18030"


def _decode_html(content: bytes, response=None) -> str:
    encoding = _detect_encoding(content, response)
    return content.decode(encoding, errors="replace")


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


def _get_book_info(soup: BeautifulSoup, page_url: str = BASE_URL) -> Dict[str, str]:
    catalog = soup.select_one("div.catalog") or soup.select_one("div.content") or soup

    title = _text(catalog.find("h1"))
    if not title:
        meta_keywords = soup.find("meta", attrs={"name": "keywords"})
        title = (meta_keywords.get("content") or "").strip() if meta_keywords else ""
    if not title:
        title_tag = soup.find("title")
        title = _clean_spaces(_text(title_tag).split(" - ", 1)[0]) if title_tag else "Unknown"

    author = "Unknown"
    info_text = _text(catalog.select_one(".info"))
    match = re.search(r"作者\s*[:：]\s*(.+)", info_text)
    if match:
        author = _clean_spaces(match.group(1))

    if author == "Unknown":
        author_link = catalog.select_one(".author a")
        if author_link:
            author = _clean_spaces(_text(author_link).replace("作品集", ""))

    if author == "Unknown":
        for writer_link in soup.select('a[href*="/files/writer/"]'):
            href = writer_link.get("href", "")
            link_text = _clean_spaces(_text(writer_link))
            if re.search(r"/files/writer/\d+\.html?$", href) or link_text.endswith("作品集"):
                author = _clean_spaces(link_text.replace("作品集", ""))
                break

    if author == "Unknown":
        page_text = _clean_spaces(soup.get_text(" ", strip=True))
        match = re.search(r"作者\s*[:：]\s*([^\s]+?)(?:\s*发布时间|\s*$)", page_text)
        if match:
            author = _clean_spaces(match.group(1))

    if author == "Unknown":
        title_tag = soup.find("title")
        title_parts = [_clean_spaces(part) for part in _text(title_tag).split(" - ") if _clean_spaces(part)] if title_tag else []
        if len(title_parts) >= 2:
            author = title_parts[1]

    intro_node = catalog.select_one(".summary .intro") or catalog.select_one(".intro")
    intro = _clean_spaces(intro_node.get_text("\n", strip=True)) if intro_node else ""
    if not intro:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        intro = _clean_spaces(meta_desc.get("content", "")) if meta_desc else ""

    cover_url = ""
    for img in catalog.find_all("img", src=True):
        src = img.get("src", "").strip()
        if not src or src.startswith("data:") or "logo" in src.lower():
            continue
        cover_url = urljoin(page_url, src)
        break

    return {
        "title": title or "Unknown",
        "author": author or "Unknown",
        "intro": intro,
        "cover_url": cover_url,
        "url": page_url,
    }


def _get_list_chapters(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    seen: set[str] = set()

    def add_chapter(title: str, href: str) -> None:
        title = _clean_spaces(title)
        if not title:
            return
        href = (href or "").strip()
        if not href or href.startswith(("javascript:", "#")):
            return
        full_url = urljoin(page_url, href)
        path = urlparse(full_url).path
        if not re.search(r"\.html?$", path, flags=re.I):
            return
        if full_url in seen:
            return
        seen.add(full_url)
        chapters.append({"title": title, "url": full_url})

    containers = soup.select("div.mulu-list")
    if not containers:
        containers = soup.select("div.catalog")

    for container in containers:
        for a in container.find_all("a", href=True):
            add_chapter(_text(a), a.get("href", ""))

    if not chapters:
        parsed_page = urlparse(page_url)
        page_path = parsed_page.path or "/"
        if page_path.endswith("/"):
            chapter_dir = page_path
        else:
            chapter_dir = page_path.rsplit("/", 1)[0] + "/"

        for a in soup.find_all("a", href=True):
            full_url = urljoin(page_url, a["href"])
            parsed_url = urlparse(full_url)
            path = parsed_url.path
            basename = path.rsplit("/", 1)[-1].lower()
            if parsed_url.netloc and parsed_url.netloc != parsed_page.netloc:
                continue
            if not path.startswith(chapter_dir):
                continue
            if basename in {"", "index.html", "index.htm"}:
                continue
            if not re.fullmatch(r"\d+\.html?", basename, flags=re.I):
                continue
            add_chapter(_text(a), full_url)

    return chapters


def _find_catalog_url_from_chapter(soup: BeautifulSoup, chapter_url: str) -> Optional[str]:
    home = soup.select_one("#BookHome[href]") or soup.select_one(".book-nav .home a[href]")
    if home:
        return urljoin(chapter_url, home["href"])
    crumb = soup.select_one(".crumb a.taxonomy[href]")
    if crumb:
        return urljoin(chapter_url, crumb["href"])
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
        "chapters": chapters,
        "total_chapters": len(chapters),
        "cover_url": info["cover_url"],
        "intro": info["intro"],
        "url": url,
    }


def _chapter_title_from_page(soup: BeautifulSoup, book_title: str = "", fallback: str = "") -> str:
    title = _text(soup.select_one(".book-content h1") or soup.find("h1"))
    if not title:
        title_tag = soup.find("title")
        title = _text(title_tag) if title_tag else ""
        parts = [p.strip() for p in title.split(" - ") if p.strip()]
        if len(parts) >= 2:
            title = parts[1]

    title = _clean_spaces(title)
    if book_title and title.startswith(book_title):
        title = title[len(book_title):].strip(" -:：")
    title = re.sub(r"^.*?\s+正文\s*", "", title).strip()
    title = re.sub(r"^正文\s*", "", title).strip()
    title = re.sub(r"\s*作者\s*[:：].*$", "", title).strip()
    return title or fallback or "Chương"


def _clean_chapter_text(raw_text: str) -> List[str]:
    raw_text = html.unescape(raw_text or "")
    raw_text = raw_text.replace("\xa0", " ")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    trash_patterns = re.compile(
        r"(努努书坊|kanunu8\.com|上一页|下一页|回目录|所属书籍|"
        r"Copyright|广告|推荐阅读|看过此书的人还喜欢)",
        flags=re.I,
    )

    paragraphs: List[str] = []
    current: List[str] = []
    for line in raw_text.split("\n"):
        line = _clean_spaces(line)
        if not line:
            if current:
                paragraphs.append("".join(current).strip())
                current = []
            continue
        if trash_patterns.search(line):
            continue
        current.append(line)
    if current:
        paragraphs.append("".join(current).strip())

    return [p for p in paragraphs if p]


def _get_chapter_content_html(soup: BeautifulSoup) -> str:
    content = (
        soup.select_one(".book-content .neirong")
        or soup.select_one("#neirong")
        or soup.select_one(".neirong")
        or soup.select_one("#Article .text")
    )
    if not content:
        candidates = []
        for selector in ("#content", "#BookText", ".chapter-content", ".text", ".p10-24", "td"):
            candidates.extend(soup.select(selector))
        candidates = [node for node in candidates if len(_clean_spaces(node.get_text(" ", strip=True))) > 50]
        if candidates:
            content = max(candidates, key=lambda node: len(_clean_spaces(node.get_text(" ", strip=True))))
    if not content:
        return "<p>(Không có nội dung)</p>"

    content = BeautifulSoup(str(content), "html.parser")
    root = content.select_one(".neirong") or content
    for node in root.find_all(["script", "style", "ins", "iframe"]):
        node.decompose()
    for ad in root.select(".ad, .ads, .ad-bottom, .google-auto-placed"):
        ad.decompose()

    paragraphs: List[str] = []
    p_nodes = root.find_all("p")
    if p_nodes:
        for p_node in p_nodes:
            p_clone = BeautifulSoup(str(p_node), "html.parser")
            for br in p_clone.find_all("br"):
                br.replace_with("\n")
            paragraphs.extend(_clean_chapter_text(p_clone.get_text("\n", strip=False)))
    else:
        for br in root.find_all("br"):
            br.replace_with("\n")
        paragraphs = _clean_chapter_text(root.get_text("\n", strip=False))

    if not paragraphs:
        return "<p>(Không có nội dung)</p>"
    return "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)


def fetch_chapter_content(url: str, retries: int = 3, *, fallback_title: str = "", book_title: str = "") -> Dict:
    last_status: Optional[int] = None
    for attempt in range(1, retries + 1):
        try:
            soup, status_code = _fetch_html_with_status(url)
            last_status = status_code
            title = _chapter_title_from_page(soup, book_title=book_title, fallback=fallback_title)
            content_html = _get_chapter_content_html(soup)
            time.sleep(SLEEP_BETWEEN_CHAPS)
            return {"title": title, "content_html": content_html, "url": url, "status_code": status_code}
        except FetchHtmlError as exc:
            last_status = exc.status_code
            if attempt < retries:
                time.sleep(SLEEP_BETWEEN_CHAPS * attempt)
        except Exception as exc:
            if attempt < retries:
                time.sleep(SLEEP_BETWEEN_CHAPS * attempt)

    return {
        "title": fallback_title or "Chương lỗi",
        "content_html": "<p>(Không tải được nội dung)</p>",
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
    article = soup.select_one("article.chapter-content") or soup.find("body") or soup
    for node in article.select(".source"):
        node.decompose()
    return {
        "title": title,
        "content_html": "\n".join(str(child) for child in article.contents).strip(),
        "url": str(html_path),
    }


def _save_chapter_txt(chapter_data: Dict[str, str], txt_path: Path) -> None:
    soup = BeautifulSoup(chapter_data.get("content_html", ""), "html.parser")
    text = soup.get_text("\n", strip=True)
    txt_path.write_text(f"{chapter_data['title']}\n\n{text}\n", encoding="utf-8")


def _chapter_html_path(book_dir: Path, idx: int) -> Path:
    return book_dir / f"chapter_{idx:04d}.html"


def _legacy_chapter_html_path(book_dir: Path, idx: int) -> Path:
    return book_dir / "html" / f"{idx:04d}.html"


def _find_cached_chapter_path(book_dir: Path, idx: int) -> Optional[Path]:
    direct_path = _chapter_html_path(book_dir, idx)
    if direct_path.exists():
        return direct_path

    legacy_path = _legacy_chapter_html_path(book_dir, idx)
    if legacy_path.exists():
        return legacy_path

    return None


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
        data["status_code"] = "CACHE"
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


def save_combined_txt(book_info: Dict[str, str], chapters: List[Dict[str, str]], book_dir: Path) -> Path:
    book_dir.mkdir(parents=True, exist_ok=True)
    out_path = book_dir / f"{_safe_filename(book_info['title'])}.txt"
    chunks: List[str] = [book_info["title"], f"作者：{book_info.get('author', 'Unknown')}", ""]
    if book_info.get("intro"):
        chunks.extend(["内容简介：", book_info["intro"], ""])

    _safe_print(f"Bắt đầu tạo TXT gộp từ {len(chapters)} chương...")
    for idx, chapter in enumerate(chapters, 1):
        html_path = _find_cached_chapter_path(book_dir, idx)
        if html_path:
            data = _read_cached_chapter(html_path)
        else:
            data = fetch_chapter_content(
                chapter["url"],
                fallback_title=chapter.get("title", f"Chương {idx}"),
                book_title=book_info.get("title", ""),
            )
            _chapter_html_path(book_dir, idx).write_text(
                _chapter_html_doc(data["title"], data["content_html"], data["url"]),
                encoding="utf-8",
            )
        text = BeautifulSoup(data["content_html"], "html.parser").get_text("\n", strip=True)
        chunks.extend([data["title"], "", text, ""])
        if idx == 1 or idx == len(chapters) or idx % 10 == 0:
            _safe_print(f"TXT gộp: đã xử lý {idx}/{len(chapters)} chương")

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
    _safe_print(f"Bắt đầu gom nội dung EPUB từ chương {start} đến {end}...")
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
            cover_xhtml = _xhtml_page(
                "Cover",
                f'<p class="cover"><img src="../{cover_name}" alt="{html.escape(title)}"/></p>',
            )
            _zip_write(zf, "OEBPS/Text/cover.xhtml", cover_xhtml)

        for order, chapter in enumerate(items, 1):
            body = f"<h1>{html.escape(chapter['title'])}</h1>\n{chapter.get('content_html') or '<p>(Không có nội dung)</p>'}"
            _zip_write(zf, f"OEBPS/Text/chapter_{order:04d}.xhtml", _xhtml_page(chapter["title"], body))

    _safe_print(f"Đã tạo EPUB thủ công: {epub_path}")
    return epub_path


def _prepare_book_dir(book_info: Dict[str, str]) -> Path:
    folder_name = _safe_filename(book_info["title"])
    book_dir = OUTPUT_BASE / folder_name
    book_dir.mkdir(parents=True, exist_ok=True)
    return book_dir


def _save_book_info(book_info: Dict[str, str], chapters: List[Dict[str, str]], book_dir: Path) -> None:
    lines = [
        f"Title: {book_info.get('title', '')}",
        f"Author: {book_info.get('author', '')}",
        f"URL: {book_info.get('url', '')}",
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
        "intro": data.get("intro", ""),
        "cover_url": data.get("cover_url", ""),
        "url": data.get("url", url),
    }
    chapters = data["chapters"]
    book_dir = _prepare_book_dir(book_info)
    _save_book_info(book_info, chapters, book_dir)
    epub_preview_path = OUTPUT_BASE / f"{_safe_filename(book_info['title'])}_{_safe_filename(book_info['author'])}.epub"

    _safe_print("\n-----------------Thông tin truyện-----------------")
    _safe_print(f"Tên truyện : {book_info['title']}")
    _safe_print(f"Tác giả    : {book_info['author']}")
    _safe_print(f"Số chương  : {len(chapters)}")
    _safe_print(f"Thư mục chương : {book_dir}")
    _safe_print(f"Mẫu file chương: {book_dir / 'chapter_0001.html'}")
    _safe_print(f"EPUB sẽ lưu   : {epub_preview_path}")
    if book_info.get("intro"):
        _safe_print(f"Giới thiệu : {book_info['intro'][:160]}{'...' if len(book_info['intro']) > 160 else ''}")

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
    _safe_print("Downloader kanunu8.com / 努努书坊")
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
                    _safe_print("\n===== Bắt đầu: Tải toàn bộ + xuất EPUB =====")
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



try:
    from download_policy import install_adapter_policy as _install_adapter_policy
    _install_adapter_policy(globals())
except Exception:
    pass
if __name__ == "__main__":
    import sys as _sys
    from adapter_cli import dispatch_or_menu as _dispatch_or_menu
    _dispatch_or_menu(_sys.modules[__name__], main, default_url=globals().get("DEFAULT_URL", ""))
