# -*- coding: utf-8 -*-
"""Downloader cho khoaitay.cc, luu tung chuong va dong goi EPUB."""

from __future__ import annotations

import argparse
import html
import io
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from epub_builder import create_epub

try:
    from PIL import Image
except ImportError:
    Image = None


BASE_URL = "https://khoaitay.cc/"
DEFAULT_URL = "https://khoaitay.cc/book/satq"
OUTPUT_BASE = Path("Output")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36", "Accept-Language": "vi,en;q=0.8"}
TIMEOUT = 30


def _safe_name(value: str, limit: int = 150) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', " - ", value or "Truyen")
    return re.sub(r"\s+", " ", value).strip().strip(".")[:limit] or "Truyen"


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _get(url: str):
    import requests
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    return response


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean(tag["content"])
    return ""


def _payload_text(soup: BeautifulSoup) -> str:
    payload = "\n".join(script.get_text() for script in soup.find_all("script") if "chapterNumber" in script.get_text())
    return payload.replace('\\"', '"')


def _chapter_links(soup: BeautifulSoup, page_url: str) -> List[Dict[str, Any]]:
    links: List[Dict[str, Any]] = []
    seen = set()
    payload = _payload_text(soup)
    pattern = r'\{"id":(\d+),"title":"((?:\\.|[^"\\])*)","chapterNumber":(\d+)'
    for match in re.finditer(pattern, payload):
        chapter_id = int(match.group(1))
        title = json.loads('"' + match.group(2) + '"')
        url = urljoin(page_url, f"/chapter/{chapter_id}")
        if url not in seen:
            links.append({"title": title, "url": url, "chapterNumber": int(match.group(3))})
            seen.add(url)
    if links:
        return sorted(links, key=lambda item: (item.get("chapterNumber", 0), item["url"]))
    for link in soup.select('a[href*="/chapter/"]'):
        url = urljoin(page_url, link["href"])
        if url not in seen:
            links.append({"title": _clean(link.get_text(" ", strip=True)), "url": url})
            seen.add(url)
    return links


def getText(url: str) -> Dict[str, Any]:
    response = _get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    heading = soup.find("h1")
    title = _meta(soup, "og:title") or _clean(heading.get_text(" ", strip=True) if heading else "Truyen")
    image = soup.find("img", src=True)
    return {
        "title": title,
        "author": _meta(soup, "og:novel:author") or "Khong ro",
        "status": _meta(soup, "og:novel:status"),
        "category": _meta(soup, "og:novel:category"),
        "intro": _meta(soup, "og:description", "description"),
        "cover_url": _meta(soup, "og:image") or (urljoin(url, image["src"]) if image else ""),
        "url": url,
        "chapters": _chapter_links(soup, url),
    }


def _content_node(soup: BeautifulSoup):
    paragraphs = soup.select(".reader-paragraph")
    if paragraphs:
        wrapper = soup.new_tag("div")
        for paragraph in paragraphs:
            wrapper.append(paragraph)
        return wrapper
    article = soup.select_one(".reader-settings-content") or soup.find("article") or soup.body or soup
    for node in article.select("script, style, nav, header, footer, button, a, form"):
        node.decompose()
    return article


def _chapter_title(soup: BeautifulSoup, page_url: str = "") -> str:
    payload = _payload_text(soup)
    chapter_id = re.search(r"/chapter/(\d+)", page_url)
    if chapter_id:
        match = re.search(r'"id":' + chapter_id.group(1) + r',"title":"((?:\\.|[^"\\])*)"', payload)
        if match:
            return json.loads('"' + match.group(1) + '"')
    match = re.search(r'"title":"((?:\\.|[^"\\])*)","chapterNumber":\d+', payload)
    if match:
        return json.loads('"' + match.group(1) + '"')
    heading = soup.find("h1")
    if heading:
        return _clean(heading.get_text(" ", strip=True))
    return _clean(soup.title.get_text(" ", strip=True) if soup.title else "Chuong")


def fetch_chapter_content(url: str, retries: int = 3, fallback_title: str = "", **_: Any) -> Dict[str, Any]:
    last_error = None
    for attempt in range(retries):
        try:
            response = _get(url)
            soup = BeautifulSoup(response.text, "html.parser")
            title = fallback_title.strip() or _chapter_title(soup, url)
            node = _content_node(soup)
            content_html = "\n".join(str(child) for child in node.contents).strip()
            text = node.get_text("\n", strip=True)
            if not text:
                raise ValueError("Khong tim thay noi dung chuong")
            return {"title": title, "content_html": content_html, "text": text, "url": url, "status_code": response.status_code}
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1 + attempt)
    raise RuntimeError(f"Khong tai duoc chuong {url}: {last_error}")


def _cover(url: str) -> Tuple[Optional[bytes], str]:
    if not url:
        return None, ".jpg"
    try:
        data = _get(url).content
        if Image is None:
            return data, ".jpg"
        image = Image.open(io.BytesIO(data)).convert("RGB")
        image.thumbnail((1600, 2400))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92)
        return output.getvalue(), ".jpg"
    except Exception as exc:
        print(f"[WARN] Khong tai duoc cover: {exc}")
        return None, ".jpg"


def _write_chapter(path: Path, chapter: Dict[str, Any]) -> None:
    body = chapter.get("content_html") or f"<p>{html.escape(chapter.get('text', ''))}</p>"
    document = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="vi"><head><meta charset="utf-8"/><title>{html.escape(chapter["title"])}</title></head><body><h1>{html.escape(chapter["title"])}</h1>{body}</body></html>'''
    path.write_text(document, encoding="utf-8")


def download_book(book_info: Dict[str, Any], *, output_base: Path = OUTPUT_BASE, delay: float = 0.3) -> Path:
    book_dir = output_base / _safe_name(book_info["title"])
    book_dir.mkdir(parents=True, exist_ok=True)
    downloaded: List[Dict[str, Any]] = []
    chapters = book_info.get("chapters", [])
    for index, chapter in enumerate(chapters, 1):
        target = book_dir / f"{index:04d}.xhtml"
        try:
            data = fetch_chapter_content(chapter["url"], fallback_title=chapter.get("title", ""))
            data["chapter_index"] = index
            _write_chapter(target, data)
            downloaded.append(data)
            print(f"[{index:04d}/{len(chapters):04d}] {data['title']}")
        except Exception as exc:
            print(f"[LOI] [{index:04d}] {chapter.get('title', chapter['url'])}: {exc}")
            downloaded.append({
                "title": chapter.get("title") or f"Chuong {index}",
                "content_html": "<p>(Khong tai duoc noi dung chuong nay)</p>",
                "text": "",
                "url": chapter["url"],
                "status_code": "ERR",
            })
        time.sleep(delay)
    (book_dir / "book_info.json").write_text(json.dumps(book_info, ensure_ascii=False, indent=2), encoding="utf-8")
    cover_bytes, cover_ext = _cover(book_info.get("cover_url", ""))
    epub_path = output_base / f"{_safe_name(book_info['title'])}_{_safe_name(book_info.get('author', 'Khong ro'))}.epub"
    create_epub(book_info["url"], book_info["title"], book_info.get("author", "Khong ro"), chapters, cover_bytes=cover_bytes, cover_ext=cover_ext, chapters_data=downloaded, out_epub_path=str(epub_path), book_info=book_info, tags=book_info.get("category"), intro=book_info.get("intro"))
    return epub_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Tai truyen tu khoaitay.cc")
    parser.add_argument("url", nargs="?", help="URL trang truyen")
    args = parser.parse_args()
    url = args.url or input(f"URL truyen [{DEFAULT_URL}]: ").strip() or DEFAULT_URL
    info = getText(url)
    print(f"{info['title']} | {info['author']} | {len(info['chapters'])} chuong")
    print(f"Da tao EPUB: {download_book(info)}")


if __name__ == "__main__":
    main()