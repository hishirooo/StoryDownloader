#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
mottruyen_downloader.py
Tải truyện từ mottruyen.com.vn và build EPUB.

Cài:
    pip install requests beautifulsoup4 lxml cloudscraper

Dùng:
    python mottruyen_downloader.py "https://mottruyen.com.vn/novel-tho-san-muon-song-an-dat/42612"

Tuỳ chọn:
    python mottruyen_downloader.py "URL" --start 1 --end 50
    python mottruyen_downloader.py "URL" --delay 0.8
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import bs4
    import cloudscraper
except ImportError:
    os.system("pip install requests beautifulsoup4 lxml cloudscraper")
    import bs4
    import cloudscraper
from epub_builder import create_epub


# =========================
# Helpers
# =========================

def clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = s.replace("\xa0", " ")
    s = re.sub(r"\r", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value.lower())
    value = re.sub(r"[-\s]+", "-", value).strip("-_")
    return value or "book"


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] if len(name) > 180 else name


def first_not_empty(*values):
    for v in values:
        if v:
            return v
    return None


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def chapter_sort_key(item: "Chapter") -> Tuple[int, str]:
    return (item.number if item.number is not None else 10**9, item.url)


# =========================
# Data models
# =========================

@dataclass
class Chapter:
    number: Optional[int]
    title: str
    url: str


@dataclass
class BookMeta:
    title: str
    author: str
    description: str
    cover_url: Optional[str]
    tags: List[str]
    status: str


# =========================
# Scraper
# =========================

class MotTruyenDownloader:
    def __init__(self, delay: float = 0.8, timeout: int = 30):
        self.delay = delay
        self.timeout = timeout
        self.scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        self.scraper.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
            "Referer": "https://mottruyen.com.vn/",
        })

    def get(self, url: str) -> str:
        resp = self.scraper.get(url, timeout=self.timeout)
        resp.raise_for_status()
        time.sleep(self.delay)
        return resp.text

    def soup(self, html_text: str) -> bs4.BeautifulSoup:
        return bs4.BeautifulSoup(html_text, "lxml")

    # ---------- meta ----------
    def parse_book_meta(self, url: str) -> Tuple[BookMeta, List[Chapter]]:
        html_text = self.get(url)
        soup = self.soup(html_text)

        title = self._extract_book_title(soup, url)
        author = self._extract_author(soup)
        description = self._extract_description(soup)
        cover_url = self._extract_cover(soup, url)
        tags = self._extract_tags(soup)
        status = self._extract_status(soup)

        chapters = self._extract_chapter_list(soup, url, title)

        if not chapters:
            # fallback: brute-force by trying chapter links if list không hiện
            chapters = self._build_chapters_by_guess(url, html_text, title)

        meta = BookMeta(
            title=title,
            author=author or "Unknown",
            description=description or "",
            cover_url=cover_url,
            tags=tags,
            status=status or "",
        )
        return meta, chapters

    def _extract_book_title(self, soup: bs4.BeautifulSoup, url: str) -> str:
        candidates = []

        og = soup.select_one('meta[property="og:title"]')
        if og and og.get("content"):
            candidates.append(og["content"].strip())

        tw = soup.select_one('meta[name="twitter:title"]')
        if tw and tw.get("content"):
            candidates.append(tw["content"].strip())

        for sel in ["h1", "h2", ".book-title", ".novel-title", ".detail-title", ".title"]:
            el = soup.select_one(sel)
            if el:
                txt = clean_text(el.get_text(" ", strip=True))
                if txt:
                    candidates.append(txt)

        # title tag
        if soup.title and soup.title.string:
            candidates.append(clean_text(soup.title.string))

        # URL fallback
        path = urlparse(url).path.strip("/").split("/")
        if path:
            candidates.append(path[0].replace("-", " ").strip())

        # cleanup
        cleaned = []
        for c in candidates:
            c = re.sub(r"^\[[^\]]+\]\s*", "", c).strip()
            c = re.sub(r"\s*-\s*Chương.*$", "", c, flags=re.I)
            c = re.sub(r"\s*\|\s*.*$", "", c)
            c = clean_text(c)
            if c and c.lower() not in {"doc truyen online", "một truyện", "mottruyen"}:
                cleaned.append(c)

        if cleaned:
            # ưu tiên chuỗi dài vừa phải, có vẻ đúng tên truyện
            cleaned = sorted(cleaned, key=lambda x: (-len(x), x))
            return cleaned[0]

        return "Unknown Title"

    def _extract_author(self, soup: bs4.BeautifulSoup) -> str:
        text = soup.get_text("\n", strip=True)

        # regex label phổ biến
        m = re.search(r"Tác giả\s*[:：]\s*(.+)", text, flags=re.I)
        if m:
            val = m.group(1).split("\n")[0].strip()
            return val[:120]

        for sel in [".author", ".book-author", ".novel-author"]:
            el = soup.select_one(sel)
            if el:
                t = clean_text(el.get_text(" ", strip=True))
                if t:
                    return t
        return "Unknown"

    def _extract_description(self, soup: bs4.BeautifulSoup) -> str:
        desc = soup.select_one('meta[name="description"]')
        if desc and desc.get("content"):
            return clean_text(desc["content"])

        og = soup.select_one('meta[property="og:description"]')
        if og and og.get("content"):
            return clean_text(og["content"])

        for sel in [".summary", ".description", ".book-summary", ".novel-summary", ".content"]:
            el = soup.select_one(sel)
            if el:
                t = clean_text(el.get_text("\n", strip=True))
                if len(t) > 30:
                    return t
        return ""

    def _extract_cover(self, soup: bs4.BeautifulSoup, base_url: str) -> Optional[str]:
        selectors = [
            'meta[property="og:image"]',
            ".book-cover img",
            ".novel-cover img",
            ".thumb img",
            "img",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if not el:
                continue
            if el.name == "meta":
                src = el.get("content")
            else:
                src = el.get("data-src") or el.get("src")
            if src and src.startswith(("http://", "https://", "/")):
                return urljoin(base_url, src)
        return None

    def _extract_tags(self, soup: bs4.BeautifulSoup) -> List[str]:
        tags = []
        for sel in [".tag a", ".tags a", ".genre a", ".category a", ".categories a"]:
            for a in soup.select(sel):
                t = clean_text(a.get_text(" ", strip=True))
                if t and t not in tags:
                    tags.append(t)
        return tags[:30]

    def _extract_status(self, soup: bs4.BeautifulSoup) -> str:
        text = soup.get_text("\n", strip=True)
        m = re.search(r"(Trạng thái|Status)\s*[:：]\s*([^\n]+)", text, flags=re.I)
        if m:
            return clean_text(m.group(2))
        if "Hoàn" in text:
            return "Hoàn"
        return ""

    # ---------- chapter list ----------
    def _extract_chapter_list(
        self, soup: bs4.BeautifulSoup, base_url: str, book_title: str
    ) -> List[Chapter]:
        chapters: List[Chapter] = []
        seen = set()

        for a in soup.select("a[href]"):
            href = a.get("href", "").strip()
            text = clean_text(a.get_text(" ", strip=True))
            full = urljoin(base_url, href)

            if "/chuong/" not in full:
                continue
            if "mottruyen.com.vn" not in full:
                continue

            num = self._extract_chapter_number(full, text)
            ch_title = text or f"Chương {num}" if num is not None else text

            # loại các link rác
            if not ch_title:
                ch_title = f"Chương {num}" if num is not None else "Chương"

            norm = re.sub(r"\s+", " ", full.strip())
            if norm in seen:
                continue

            seen.add(norm)
            chapters.append(Chapter(number=num, title=ch_title, url=norm))

        chapters.sort(key=chapter_sort_key)

        # lọc bớt các link điều hướng kỳ quặc
        filtered = []
        for ch in chapters:
            if ch.number is not None and ch.number < 0:
                continue
            filtered.append(ch)

        return filtered

    def _build_chapters_by_guess(self, book_url: str, html_text: str, book_title: str) -> List[Chapter]:
        """
        Fallback khi trang không render list chương rõ ràng.
        Dò số chapter lớn nhất từ HTML/title/snippets nếu có, rồi tự sinh URL.
        """
        text = clean_text(bs4.BeautifulSoup(html_text, "lxml").get_text("\n", strip=True))

        nums = []
        for m in re.finditer(r"Chương\s+(\d+)\s*/\s*(\d+)", text, flags=re.I):
            nums.append(int(m.group(2)))
        for m in re.finditer(r"(\d+)\s+chương", text, flags=re.I):
            nums.append(int(m.group(1)))

        max_ch = max(nums) if nums else None

        # fallback URL slug
        parsed = urlparse(book_url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2:
            return []

        slug = parts[0]
        book_id = parts[1]

        if max_ch is None:
            # đoán an toàn nếu không tìm được
            max_ch = 500

        chapters = []
        for i in range(1, max_ch + 1):
            ch_url = f"{parsed.scheme}://{parsed.netloc}/{slug}/{book_id}/chuong/{i}"
            chapters.append(Chapter(number=i, title=f"Chương {i}", url=ch_url))
        return chapters

    def _extract_chapter_number(self, url: str, text: str = "") -> Optional[int]:
        m = re.search(r"/chuong/(\d+)", url)
        if m:
            return int(m.group(1))
        m = re.search(r"Chương\s+(\d+)", text, flags=re.I)
        if m:
            return int(m.group(1))
        return None

    # ---------- chapter content ----------
    def fetch_chapter_content(self, chapter: Chapter) -> Tuple[str, str]:
        html_text = self.get(chapter.url)
        soup = self.soup(html_text)

        title = self._extract_chapter_title(soup, chapter)
        content_html = self._extract_chapter_html(soup)

        if not content_html:
            # fallback thô: lấy text từ body, cố loại header/footer
            content_html = self._fallback_body_to_html(soup)

        if not content_html:
            raise RuntimeError(f"Không lấy được nội dung chapter: {chapter.url}")

        return title, content_html

    def _extract_chapter_title(self, soup: bs4.BeautifulSoup, chapter: Chapter) -> str:
        candidates = []

        og = soup.select_one('meta[property="og:title"]')
        if og and og.get("content"):
            candidates.append(og["content"])

        for sel in ["h1", "h2", ".chapter-title", ".title", ".detail-title"]:
            el = soup.select_one(sel)
            if el:
                t = clean_text(el.get_text(" ", strip=True))
                if t:
                    candidates.append(t)

        for c in candidates:
            c = re.sub(r"^\[[^\]]+\]\s*", "", c).strip()
            c = re.sub(r".*-\s*(Chương\s*\d+.*)$", r"\1", c, flags=re.I)
            c = clean_text(c)
            if c:
                return c

        if chapter.number is not None:
            return f"Chương {chapter.number}"
        return chapter.title or "Chương"

    def _extract_chapter_html(self, soup: bs4.BeautifulSoup) -> str:
        candidates = [
            ".chapter-content",
            ".content-reading",
            ".reading-content",
            ".chapter-detail",
            ".chapter-body",
            ".entry-content",
            ".content",
            "article",
            ".post-content",
        ]

        for sel in candidates:
            el = soup.select_one(sel)
            if not el:
                continue
            html_content = self._normalize_content_element(el)
            if self._looks_like_real_chapter(html_content):
                return html_content

        return ""

    def _normalize_content_element(self, el: bs4.Tag) -> str:
        # xóa script/style/ads
        for bad in el.select("script, style, iframe, ins, ads, .ads, .advertisement, .google-auto-placed"):
            bad.decompose()

        # remove comment-ish blocks / app prompt
        for bad in el.find_all(string=re.compile(r"Để đọc tiếp|mở app|ủng hộ Mọt|Shopee", re.I)):
            parent = bad.parent
            if parent and getattr(parent, "name", None) in {"p", "div", "span"}:
                parent.decompose()

        paragraphs = []
        for node in el.find_all(["p", "div"]):
            txt = clean_text(node.get_text("\n", strip=True))
            if not txt:
                continue
            if re.search(r"^(Trang chủ|Đăng nhập|Đăng ký|Bảo mật|FAQ|Liên hệ|Điều khoản)$", txt, re.I):
                continue
            if re.search(r"Để đọc tiếp|mở app|ủng hộ Mọt|Shopee", txt, re.I):
                continue
            paragraphs.append(f"<p>{html.escape(txt)}</p>")

        if not paragraphs:
            txt = clean_text(el.get_text("\n", strip=True))
            if txt:
                lines = [x.strip() for x in txt.splitlines() if x.strip()]
                paragraphs = [
                    f"<p>{html.escape(line)}</p>"
                    for line in lines
                    if not re.search(r"Để đọc tiếp|mở app|ủng hộ Mọt|Shopee", line, re.I)
                ]

        return "\n".join(paragraphs)

    def _looks_like_real_chapter(self, content_html: str) -> bool:
        text = clean_text(bs4.BeautifulSoup(content_html, "lxml").get_text("\n", strip=True))
        if len(text) < 150:
            return False
        if text.count(" ") < 40:
            return False
        return True

    def _fallback_body_to_html(self, soup: bs4.BeautifulSoup) -> str:
        body = soup.body
        if not body:
            return ""
        text = clean_text(body.get_text("\n", strip=True))
        lines = [x.strip() for x in text.splitlines() if x.strip()]

        bad_patterns = [
            r"^doc truyen online$",
            r"^Đăng nhập$",
            r"^Đăng ký$",
            r"^Trang chủ$",
            r"^Bảo mật$",
            r"^FAQ$",
            r"^Liên hệ$",
            r"^Điều khoản$",
            r"^Mottruyen.vn$",
            r"Để đọc tiếp",
            r"mở app",
            r"ủng hộ Mọt",
            r"Shopee",
        ]

        kept = []
        for line in lines:
            if any(re.search(p, line, re.I) for p in bad_patterns):
                continue
            kept.append(line)

        if len(" ".join(kept)) < 150:
            return ""

        return "\n".join(f"<p>{html.escape(x)}</p>" for x in kept)


# =========================
# EPUB Builder
# =========================

class EpubBuilder:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        ensure_dir(output_dir)

    def download_cover(self, downloader: MotTruyenDownloader, cover_url: Optional[str]) -> Optional[bytes]:
        if not cover_url:
            return None
        try:
            resp = downloader.scraper.get(cover_url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except Exception:
            return None

    @staticmethod
    def _cover_ext(cover_bytes: Optional[bytes], cover_url: Optional[str]) -> str:
        if cover_bytes:
            if cover_bytes.startswith(b"\x89PNG"):
                return ".png"
            if cover_bytes.startswith(b"GIF87a") or cover_bytes.startswith(b"GIF89a"):
                return ".gif"
            if cover_bytes[:12].startswith(b"RIFF") and cover_bytes[8:12] == b"WEBP":
                return ".webp"
        ext = Path(urlparse(cover_url or "").path).suffix.lower()
        return ext if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"

    def build(
        self,
        meta: BookMeta,
        chapters_data: List[Tuple[Chapter, str, str]],
        cover_bytes: Optional[bytes] = None,
    ) -> Path:
        out_name = safe_filename(meta.title) + ".epub"
        out_path = self.output_dir / out_name

        chapters = [{"title": title, "url": chapter.url} for chapter, title, _content_html in chapters_data]
        items = [
            {"title": title, "content_html": content_html, "url": chapter.url}
            for chapter, title, content_html in chapters_data
        ]
        book_info = {
            "title": meta.title,
            "author": meta.author or "Unknown",
            "description": meta.description or "",
            "status": meta.status or "",
            "tags": meta.tags,
            "cover_url": meta.cover_url or "",
        }

        create_epub(
            book_url="",
            book_title=meta.title,
            author=meta.author or "Unknown",
            chapters=chapters,
            fetch_fn=None,
            cover_bytes=cover_bytes,
            cover_ext=self._cover_ext(cover_bytes, meta.cover_url),
            language="vi",
            out_epub_path=str(out_path),
            chapters_data=items,
            tags=meta.tags,
            book_info=book_info,
        )
        return out_path


# =========================
# Main
# =========================

def main():
    parser = argparse.ArgumentParser(description="Download truyện từ mottruyen.com.vn và build EPUB")
    parser.add_argument("url", help="URL truyện")
    parser.add_argument("--start", type=int, default=None, help="Chương bắt đầu")
    parser.add_argument("--end", type=int, default=None, help="Chương kết thúc")
    parser.add_argument("--delay", type=float, default=0.8, help="Delay giữa các request")
    parser.add_argument("--out", default="output_mottruyen", help="Thư mục output")
    parser.add_argument("-y", "--yes", action="store_true", help="Run non-interactively")
    args = parser.parse_args()

    out_dir = Path(args.out)
    ensure_dir(out_dir)

    downloader = MotTruyenDownloader(delay=args.delay)
    builder = EpubBuilder(out_dir)

    print("=" * 70)
    print("MOTTRUYEN DOWNLOADER")
    print("=" * 70)
    print(f"URL: {args.url}")

    try:
        meta, chapters = downloader.parse_book_meta(args.url)
    except Exception as e:
        print(f"[ERROR] Không lấy được metadata: {e}")
        sys.exit(1)

    chapters = sorted(chapters, key=chapter_sort_key)

    # lọc theo range
    if args.start is not None:
        chapters = [c for c in chapters if c.number is None or c.number >= args.start]
    if args.end is not None:
        chapters = [c for c in chapters if c.number is None or c.number <= args.end]

    # nếu vẫn bị quá nhiều link rác, ưu tiên chapter có số
    numbered = [c for c in chapters if c.number is not None]
    if len(numbered) >= max(5, len(chapters) // 2):
        chapters = numbered

    print(f"Title      : {meta.title}")
    print(f"Author     : {meta.author}")
    print(f"Status     : {meta.status}")
    print(f"Tags       : {', '.join(meta.tags[:10])}")
    print(f"Cover      : {meta.cover_url or 'N/A'}")
    print(f"Chapters   : {len(chapters)}")

    if not chapters:
        print("[ERROR] Không tìm được danh sách chapter.")
        sys.exit(1)

    import download_policy

    chapter_by_url = {chapter.url: chapter for chapter in chapters}

    def fetch_one(url: str, retries: int = 1, fallback_title: str = "", book_title: str = "") -> Dict[str, str]:
        chapter = chapter_by_url[url]
        ch_title, ch_html = downloader.fetch_chapter_content(chapter)
        return {
            "title": ch_title or fallback_title,
            "content_html": ch_html,
            "text": BeautifulSoup(ch_html or "", "html.parser").get_text("\n", strip=True),
            "url": url,
            "status_code": 200,
        }

    policy_chapters = [{"title": chapter.title, "url": chapter.url} for chapter in chapters]
    policy_result = download_policy.download_chapters_with_retries(
        module=downloader,
        book_title=meta.title,
        chapters=policy_chapters,
        out_dir=out_dir / safe_filename(meta.title),
        fetch_fn=fetch_one,
        start=1,
        end=None,
        book_url=args.url,
    )

    chapters_data: List[Tuple[Chapter, str, str]] = []
    for data_item in policy_result["chapters_data"]:
        chapter = chapter_by_url.get(data_item.get("url"))
        if chapter:
            chapters_data.append((chapter, data_item.get("title") or chapter.title, data_item.get("content_html") or ""))

    failed = policy_result["failures"]
    if not chapters_data:
        print("[ERROR] Không tải được chapter nào.")
        sys.exit(1)

    cover_bytes = builder.download_cover(downloader, meta.cover_url)
    epub_path = builder.build(meta, chapters_data, cover_bytes=cover_bytes)

    print("\n" + "=" * 70)
    print(f"EPUB OK: {epub_path}")
    print(f"Tải thành công: {len(chapters_data)} chương")
    print(f"Lỗi          : {len(failed)} chương")

    if failed:
        print("\nDanh sách chương lỗi:")
        for item in failed[:20]:
            print(f" - {item.get('url')} -> {item.get('status_code')} {item.get('error')}")



try:
    from download_policy import install_adapter_policy as _install_adapter_policy
    _install_adapter_policy(globals())
except Exception:
    pass
if __name__ == "__main__":
    main()
