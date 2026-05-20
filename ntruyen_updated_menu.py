# -*- coding: utf-8 -*-
"""
ntruyen.py - Downloader + EPUB builder cho ntruyen.biz (text-only).

Mục tiêu:
- Lấy novelId từ trang /truyen/<slug> (nếu bị 403 có thể nhập novelId thủ công).
- Gọi API lấy danh sách chương: https://api.ntruyen.biz/novels/{novelId}/chapters
- Tải nội dung chương từ trang /doc-truyen/... và bóc nội dung trong self.__next_f.push(...)
- Đóng gói EPUB thủ công (EPUB3) dựa trên logic trong phongphongtam2_ver2.py (bububaoboi style).

Ví dụ URL chương:
    doc_base_url = https://ntruyen.biz/doc-truyen/canh-cua-trong-khe-nut-matthia
    chapter_slug = chuong-1
    chapter_id   = 4521179
 -> https://ntruyen.biz/doc-truyen/canh-cua-trong-khe-nut-matthia-chuong-1-4521179

Yêu cầu:
    pip install requests beautifulsoup4
    (khuyên) pip install pillow   # để convert/resize cover tốt hơn
"""

from __future__ import annotations

from typing import Optional, List, Dict, Tuple
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse
from download_logger import chapter_log_line
import requests
import re
import json
from epub_metadata import subject_xml
import os
import unicodedata
import zipfile
import html as _html
import time

# ========================= CẤU HÌNH =========================

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
TIMEOUT = (10, 35)  # (connect, read) seconds
CHAPTER_TIMEOUT = (15, 90)  # timeout riêng cho doc-truyen chương
RETRY_STATUS = {429, 500, 502, 503, 504}

# Mỗi page API chapters trả 50 item là hợp lý
API_LIMIT = 50
API_SORT = "asc"

EPUB_TARGET = "epub3"
PUBLISHER = "Hishiro"
LANGUAGE = "vi"

# Chỗ lưu output
OUTPUT_ROOT = os.path.join(os.getcwd(), "output")

# Cover: giới hạn kích thước thân thiện cho e-reader
MAX_COVER_SIZE = (1600, 2400)

# ========================= REQUEST SESSION =========================

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "user-agent": UA,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "vi,en-US;q=0.9,en;q=0.8",
    })
    return s

def _try_get(session: requests.Session, url: str, *, headers: Optional[dict] = None,
             params: Optional[dict] = None, allow_redirects: bool = True, timeout=None) -> requests.Response:
    last_exc = None
    for attempt in range(1, 6):
        try:
            r = session.get(url, headers=headers, params=params, timeout=(timeout or TIMEOUT), allow_redirects=allow_redirects)
            if r.status_code in RETRY_STATUS:
                time.sleep(0.6 * attempt)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            time.sleep(0.6 * attempt)
    raise RuntimeError(f"GET thất bại: {url} ({last_exc})")

def _fetch_soup(session: requests.Session, url: str) -> BeautifulSoup:
    r = _try_get(session, url)
    return BeautifulSoup(r.text, "html.parser")

# ========================= TIỆN ÍCH TEXT/FILE =========================

def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""

def _slug_folder(s: str) -> str:
    s = (s or "Truyen").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or "Truyen"

def _slugify_vi(s: str) -> str:
    s = (s or "book").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-_")
    return (s or "book")[:80]

def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

# ========================= COVER (TRÍCH TỪ phongphongtam2_ver2.py) =========================

try:
    from PIL import Image  # type: ignore
    HAS_PILLOW = True
except Exception:
    HAS_PILLOW = False

def _sniff_image_type(data: bytes) -> Tuple[str, str]:
    """Trả về (ext, mime) dựa trên magic bytes."""
    if not data or len(data) < 12:
        return (".bin", "application/octet-stream")
    if data[:3] == b"\xff\xd8\xff":
        return (".jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return (".png", "image/png")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return (".webp", "image/webp")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return (".gif", "image/gif")
    return (".bin", "application/octet-stream")

def _ensure_jpeg_cover(img_bytes: bytes) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Convert cover về JPEG + resize (nếu có Pillow)."""
    if not img_bytes:
        return (None, None, None)

    ext, mime = _sniff_image_type(img_bytes)
    if not HAS_PILLOW:
        return (img_bytes, ext, mime)

    try:
        import io as _io
        im = Image.open(_io.BytesIO(img_bytes))
        resample_filter = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS

        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, resample_filter)

        # về RGB nền trắng
        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")

        out = _io.BytesIO()
        im.save(out, format="JPEG", quality=92)
        return (out.getvalue(), ".jpg", "image/jpeg")
    except Exception:
        # fallback ảnh gốc
        return (img_bytes, ext, mime)

def _load_cover_from_url(session: requests.Session, cover_url: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    if not cover_url:
        return (None, None, None)
    try:
        r = _try_get(session, cover_url, headers={"user-agent": UA, "accept": "image/*,*/*"})
        return _ensure_jpeg_cover(r.content)
    except Exception:
        return (None, None, None)

# ========================= EPUB3 PACKAGER (TRÍCH TỪ phongphongtam2_ver2.py) =========================


def _load_cover_from_input(
    session: requests.Session,
    cover_in: str,
    *,
    fallback_url: Optional[str] = None,
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Load cover theo input của người dùng.

    - cover_in = ""  -> dùng fallback_url (auto cover từ site) nếu có
    - cover_in là URL -> download ảnh
    - cover_in là đường dẫn file local -> đọc bytes
    Trả về (bytes, ext, mime) sau khi ép JPEG cho an toàn.
    """
    cover_in = (cover_in or "").strip()

    if not cover_in:
        if fallback_url:
            return _load_cover_from_url(session, fallback_url)
        return (None, None, None)

    if cover_in.lower().startswith(("http://", "https://")):
        r = _try_get(session, cover_in, headers={"user-agent": UA, "accept": "image/*,*/*"})
        return _ensure_jpeg_cover(r.content)

    p = Path(cover_in)
    if p.is_file():
        return _ensure_jpeg_cover(p.read_bytes())

    # Không nhận dạng được -> fallback
    if fallback_url:
        return _load_cover_from_url(session, fallback_url)
    return (None, None, None)


def _epub_write(zipf: zipfile.ZipFile, arcname: str, data_bytes: bytes, compress: bool = True) -> None:
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    zipf.writestr(zinfo, data_bytes)

def create_epub_epub3_for_kobo(
    book_title: str,
    author: str,
    items: list,
    out_epub_dir: str,
    cover_bytes: Optional[bytes] = None,
    cover_ext: Optional[str] = None,
    cover_mime: Optional[str] = None,
    language: str = LANGUAGE,
    publisher: str = PUBLISHER,
    tags=None,
) -> str:
    """
    Đóng gói EPUB3 thủ công.
    items: [{"title": "...", "xhtml_content": "<html...>...</html>"}]
    """
    _ensure_dir(out_epub_dir)
    out_file = os.path.join(out_epub_dir, f"{_slugify_vi(book_title)}.epub")
    cover_rel = f"Images/cover{cover_ext}" if (cover_bytes and cover_ext) else None

    manifest_items: List[str] = []
    spine_items: List[str] = []
    toc_entries: List[Tuple[str, str]] = []

    with zipfile.ZipFile(out_file, "w") as z:
        # 1) mimetype & container
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        container_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n'
            '</container>'
        ).encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)

        # 2) CSS (tối giản)
        css = (
            "body{font-family:serif;line-height:1.6;}\n"
            "h1{font-size:1.4em; margin:0.8em 0;}\n"
            "p{margin:0.6em 0;}\n"
        ).encode("utf-8")
        _epub_write(z, "OEBPS/Styles/style.css", css)

        # 3) Chapters
        for i, c in enumerate(items, 1):
            fn = f"Text/chap{i:04d}.xhtml"

            chap_soup = BeautifulSoup(c.get("xhtml_content") or "", "html.parser")
            node = chap_soup.find("section", id="chapter")
            if not node:
                node = chap_soup.find("body")

            h1_title = _text(chap_soup.find("h1"))
            content_html = "".join(str(x) for x in node.children) if node else ""

            xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">\n'
                "<head>\n"
                '  <meta charset="utf-8"/>\n'
                '  <link href="../Styles/style.css" rel="stylesheet" type="text/css"/>\n'
                "</head>\n"
                "<body>\n"
                f"  <h1>{_html.escape(h1_title or c.get('title') or f'Chương {i}')}</h1>\n"
                f"  <div>{content_html or ''}</div>\n"
                "</body>\n"
                "</html>"
            ).encode("utf-8")

            _epub_write(z, f"OEBPS/{fn}", xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            toc_entries.append((fn, c.get("title") or f"Chương {i}"))

        # 4) Cover image
        cover_item_line = ""
        if cover_rel and cover_bytes:
            _epub_write(z, f"OEBPS/{cover_rel}", cover_bytes)
            cover_item_line = (
                f'<item id="cover-img" href="{cover_rel}" media-type="{cover_mime or "image/jpeg"}" properties="cover-image"/>'
            )

        # 5) nav.xhtml
        nav_items = "\n".join(
            f'      <li><a href="{_html.escape(href)}">{_html.escape(title)}</a></li>'
            for href, title in toc_entries
        )
        nav_xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" xmlns:epub="http://www.idpf.org/2007/ops">\n'
            "<head>\n"
            '  <meta charset="utf-8"/>\n'
            "  <title>Table of Contents</title>\n"
            '  <link href="Styles/style.css" rel="stylesheet" type="text/css"/>\n'
            "</head>\n"
            "<body>\n"
            '  <nav epub:type="toc" id="toc">\n'
            "    <h1>Mục lục</h1>\n"
            "    <ol>\n"
            f"{nav_items}\n"
            "    </ol>\n"
            "  </nav>\n"
            "</body>\n"
            "</html>"
        ).encode("utf-8")
        _epub_write(z, "OEBPS/nav.xhtml", nav_xhtml)
        manifest_items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')

        # 6) content.opf
        uid = _slugify_vi(book_title) + "-id"
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        subjects = subject_xml(tags, indent="  ")

        metadata = (
            f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'  <dc:identifier id="BookId">{_html.escape(uid)}</dc:identifier>\n'
            f'  <dc:title>{_html.escape(book_title)}</dc:title>\n'
            f'  <dc:language>{_html.escape(language)}</dc:language>\n'
            f'  <dc:creator>{_html.escape(author or "Unknown")}</dc:creator>\n'
            f'  <dc:publisher>{_html.escape(publisher)}</dc:publisher>\n'
            f'{subjects}'
            f'  <meta property="dcterms:modified">{_html.escape(now_iso)}</meta>\n'
        )
        if cover_item_line:
            metadata += '  <meta name="cover" content="cover-img"/>\n'
        metadata += "</metadata>\n"

        manifest = "\n".join(
            [
                '<item id="css" href="Styles/style.css" media-type="text/css"/>',
                *manifest_items,
                *( [cover_item_line] if cover_item_line else [] ),
            ]
        )

        spine = "\n".join(spine_items)

        opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="BookId">\n'
            f"{metadata}"
            "  <manifest>\n"
            f"{manifest}\n"
            "  </manifest>\n"
            '  <spine>\n'
            f"{spine}\n"
            "  </spine>\n"
            "</package>"
        ).encode("utf-8")
        _epub_write(z, "OEBPS/content.opf", opf)

    return out_file

# ========================= NTRUYEN: novelId / chapters / content =========================

API_BASE = "https://api.ntruyen.biz"

def _normalize_url(url: str) -> str:
    """Chuẩn hóa bỏ fragment, giữ scheme/host/path/query."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), p.params, p.query, ""))

def parse_novel_slug(book_url: str) -> str:
    u = _normalize_url(book_url)
    p = urlparse(u)
    parts = [x for x in p.path.split("/") if x]
    # /truyen/<slug>
    if len(parts) >= 2 and parts[0] in ("truyen", "doc-truyen"):
        return parts[1]
    # fallback: lấy phần cuối
    return parts[-1] if parts else ""

def make_doc_base_url(book_url: str) -> str:
    """Từ https://ntruyen.biz/truyen/<slug> -> https://ntruyen.biz/doc-truyen/<slug>"""
    slug = parse_novel_slug(book_url)
    return f"https://ntruyen.biz/doc-truyen/{slug}"

def get_novel_id_from_html(html_text: str) -> int:
    """
    Bắt novelId trong source.
    Ví dụ chuỗi:
        {"novelId":9428}
        "novelId":39390
        \"novelId\":39390
    """
    patterns = [
        r'\"novelId\"\s*:\s*(\d+)',
        r'novelId\\\":\s*(\d+)',   # trường hợp đã escape
        r'\{\s*\"novelId\"\s*:\s*(\d+)\s*\}',
    ]
    for pat in patterns:
        m = re.search(pat, html_text)
        if m:
            return int(m.group(1))
    raise RuntimeError("Không tìm thấy novelId trong HTML")

def fetch_novel_info_by_api(session: requests.Session, novel_id: int) -> Dict[str, str]:
    """
    Thử lấy info truyện qua API (đỡ phụ thuộc HTML /truyen nếu bị 403).
    Endpoint dự đoán: /novels/{id}
    """
    url = f"{API_BASE}/novels/{novel_id}"
    r = _try_get(session, url, headers={"accept": "application/json, text/plain, */*"})
    data = r.json() if r.text else {}
    # cố gắng map key linh hoạt
    title = (data.get("title") or data.get("name") or data.get("novelName") or "").strip()
    author = (data.get("author") or data.get("authorName") or "").strip()
    cover = (data.get("cover") or data.get("coverUrl") or data.get("thumbnail") or "").strip()
    status = (data.get("status") or data.get("state") or "").strip()
    if not title:
        title = f"novel-{novel_id}"
    return {"title": title, "author": author or "Unknown", "cover": cover, "status": status}

def fetch_novel_info_from_book_page(session: requests.Session, book_url: str) -> Dict[str, str]:
    """Lấy title/author/cover từ HTML (khi truy cập được /truyen)."""
    soup = _fetch_soup(session, book_url)

    # title
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title:
        h1 = soup.find("h1")
        title = _text(h1)

    # cover
    cover = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        cover = og_img["content"].strip()
    if not cover:
        img = soup.find("img")
        if img and img.get("src"):
            cover = img["src"].strip()

    # author (heuristic)
    author = "Unknown"
    # thử tìm thẻ có chữ Tác giả
    txt = soup.get_text("\n", strip=True)
    m = re.search(r"Tác giả\s*[:：]\s*(.+)", txt)
    if m:
        author = m.group(1).split("\n")[0].strip()

    return {"title": title or parse_novel_slug(book_url), "author": author, "cover": cover, "status": ""}

def fetch_all_chapters(session: requests.Session, novel_id: int, *, limit: int = API_LIMIT, sort: str = API_SORT) -> Dict[str, object]:
    """
    Trả về dict:
        {
          "chapters": [ {id,name,slug}, ... ],
          "total": int,
          "totalPages": int
        }
    """
    all_chaps: List[Dict[str, object]] = []
    page = 1
    total_pages = 1
    total = 0

    while page <= total_pages:
        url = f"{API_BASE}/novels/{novel_id}/chapters"
        params = {"page": page, "keyword": "", "limit": str(limit), "sort": sort}
        headers = {
            "accept": "application/json, text/plain, */*",
            "origin": "https://ntruyen.biz",
            "referer": "https://ntruyen.biz/",
            "user-agent": UA,
        }
        r = _try_get(session, url, headers=headers, params=params)
        data = r.json()
        chaps = data.get("chapters") or []
        total = int(data.get("total") or total or 0)
        total_pages = int(data.get("totalPages") or total_pages or 1)

        for c in chaps:
            # normalize
            all_chaps.append({
                "id": c.get("id"),
                "name": c.get("name") or c.get("title") or "",
                "slug": c.get("slug") or "",
            })

        page += 1
        time.sleep(0.15)  # nhẹ nhàng thôi

    return {"chapters": all_chaps, "total": total, "totalPages": total_pages}

# Regex bắt chuỗi trong self.__next_f.push([1,"..."])
_NEXT_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[\s*1\s*,\s*"((?:\\.|[^"\\])*)"\s*\]\)', re.S)


def _json_unescape_js_string(s: str) -> str:
    """Decode a JS/JSON string content captured from self.__next_f.push([...,"..."])."""
    if s is None:
        return ""
    # The capture contains backslash escapes like \u003c, \n, \\"
    # Wrap into a JSON string and let json.loads handle the unescaping safely.
    try:
        return json.loads(f'"{s}"')
    except Exception:
        # Fallback: best-effort
        try:
            return bytes(s, "utf-8").decode("unicode_escape", errors="ignore")
        except Exception:
            return s

def sanitize_html_fragment_for_xhtml(fragment: str) -> str:
    """Make HTML fragment safer to embed inside XHTML (self-closing tags, remove scripts)."""
    if not fragment:
        return ""
    fragment = fragment.strip()

    # Parse with BeautifulSoup to normalize tag nesting and convert <br> -> <br/>
    soup = BeautifulSoup(fragment, "html.parser")

    # Drop scripts/styles/noscript
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Extract only the body contents if bs4 wrapped it
    nodes = soup.body.contents if soup.body else soup.contents
    out = "".join(str(n) for n in nodes).strip()

    # Force XHTML-friendly empty tags
    out = re.sub(r"<br\s*>", "<br/>", out, flags=re.I)
    out = re.sub(r"<hr\s*>", "<hr/>", out, flags=re.I)
    out = re.sub(r"<img\b([^>]*?)(?<!/)>", r"<img\1/>", out, flags=re.I)

    # Replace &nbsp; to plain space to avoid entity issues in XML readers
    out = out.replace("&nbsp;", " ")

    return out.strip()



def extract_chapter_html_from_doc_page(html_text: str) -> str:
    """Trích nội dung chương từ trang /doc-truyen (Next.js).

    Trang ntruyen thường nhúng nội dung chương trong các script:
      self.__next_f.push([1,"\u003cp\u003e..."])
    Hàm này sẽ decode đúng UTF-8 (không gây lỗi font) và trả về HTML fragment (đã sanitize).
    """
    if not html_text:
        return ""

    candidates: list[str] = []
    for raw in _NEXT_PUSH_RE.findall(html_text):
        decoded = _json_unescape_js_string(raw)
        if decoded:
            candidates.append(decoded)

    # Ưu tiên đoạn có nhiều <p> (nội dung chương)
    best = ""
    best_score = -1
    for c in candidates:
        score = c.count("<p") + c.count("<br") + c.count("<img")
        if score > best_score:
            best_score = score
            best = c

    if not best:
        return ""

    return sanitize_html_fragment_for_xhtml(best)

def fetch_chapter_content(
    session: requests.Session,
    chapter_url: str,
    *,
    debug_dir: Optional[str] = None,
    referer: Optional[str] = None,
) -> Tuple[str, str]:
    """Tải 1 chương từ URL /doc-truyen/... và trả về (html_fragment, plain_text)."""
    headers = {
        "user-agent": UA,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "vi,en-US;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "pragma": "no-cache",
    }
    if referer:
        headers["referer"] = referer

    resp = _try_get(session, chapter_url, headers=headers, timeout=CHAPTER_TIMEOUT)
    resp.raise_for_status()
    # ép utf-8 để tránh lỗi mojibake khi server thiếu charset
    resp.encoding = "utf-8"

    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        (Path(debug_dir) / "last_doc_truyen.html").write_text(resp.text, encoding="utf-8")

    content_html = extract_chapter_html_from_doc_page(resp.text)
    if not content_html:
        return "", ""

    # text thuần (dùng debug / preview)
    text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
    return content_html, text


def html_fragment_to_xhtml_fragment(fragment: str) -> str:
    """
    Chuyển HTML fragment sang dạng 'an toàn' cho XHTML (EPUB):
    - Tự đóng thẻ (br/img/...) bằng BeautifulSoup
    - Loại bỏ script/style
    - Giảm rủi ro lỗi XML khi mở file .xhtml
    """
    soup = BeautifulSoup(fragment or "", "html.parser")
    for bad in soup(["script", "style"]):
        bad.decompose()
    cleaned = soup.decode_contents()
    # một vài entity hay gây lỗi XML
    cleaned = cleaned.replace("&nbsp;", "&#160;")
    return cleaned.strip()

def make_chapter_url(doc_base_url: str, chapter_slug: str, chapter_id: int) -> str:
    base = doc_base_url.rstrip("/")
    return f"{base}-{chapter_slug}-{chapter_id}"

def make_chapter_xhtml(chapter_title: str, content_html: str, language: str = LANGUAGE) -> str:
    # Đưa vào <section id="chapter"> để create_epub... bóc chuẩn
    safe_fragment = html_fragment_to_xhtml_fragment(content_html)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">\n'
        "<head>\n"
        '  <meta charset="utf-8"/>\n'
        f"  <title>{_html.escape(chapter_title)}</title>\n"
        "</head>\n"
        "<body>\n"
        f"  <h1>{_html.escape(chapter_title)}</h1>\n"
        f'  <section id="chapter">\n{safe_fragment}\n  </section>\n'
        "</body>\n"
        "</html>\n"
    )

# ========================= MAIN FLOW =========================

def download_and_build_epub_ntruyen(
    book_url: str,
    *,
    novel_id: Optional[int] = None,
    out_dir: str = OUTPUT_ROOT,
    cover_in: str = "",
    limit: int = API_LIMIT,
    sort: str = API_SORT,
    sleep_between_chaps: float = 0.25,
) -> str:
    """
    Flow:
    1) Lấy novelId (từ HTML /truyen nếu truy cập được) hoặc dùng novel_id truyền vào.
    2) Lấy info truyện (ưu tiên API /novels/{id}, fallback HTML).
    3) Lấy danh sách chương qua API /chapters.
    4) Tải nội dung từng chương từ /doc-truyen và đóng gói EPUB.

    Trả về đường dẫn file epub.
    """
    session = _make_session()
    book_url = _normalize_url(book_url)

    if novel_id is None:
        # thử lấy từ /truyen page
        try:
            r = _try_get(session, book_url, headers={"user-agent": UA, "accept": "text/html,*/*"})
            novel_id = get_novel_id_from_html(r.text)
        except Exception as e:
            raise RuntimeError(
                f"Không lấy được novelId từ trang truyện (có thể bị 403).\n"
                f"Bạn hãy tự lấy novelId trong view-source (search 'novelId') rồi truyền vào tham số novel_id.\n"
                f"Lỗi: {e}"
            )

    assert novel_id is not None
    doc_base_url = make_doc_base_url(book_url)

    # Info
    info = None
    try:
        info = fetch_novel_info_by_api(session, novel_id)
    except Exception:
        try:
            info = fetch_novel_info_from_book_page(session, book_url)
        except Exception:
            info = {"title": parse_novel_slug(book_url) or f"novel-{novel_id}", "author": "Unknown", "cover": "", "status": ""}

    title = info.get("title") or f"novel-{novel_id}"
    author = info.get("author") or "Unknown"
    cover_url = info.get("cover") or ""

    print("-------------- INFO --------------")
    print("Title :", title)
    print("Author:", author)
    print("NovelId:", novel_id)
    print("Doc base:", doc_base_url)
    print("Cover :", cover_url or "(none)")
    print("----------------------------------")

    # Chapters
    chap_data = fetch_all_chapters(session, novel_id, limit=limit, sort=sort)
    chapters = chap_data["chapters"]
    if not chapters:
        raise RuntimeError("Không lấy được danh sách chương từ API.")

    print(f"Total chapters: {len(chapters)} (pages={chap_data.get('totalPages')})")

    # Cover
    cover_bytes, cover_ext, cover_mime = _load_cover_from_input(session, cover_in, fallback_url=cover_url)

    # Output folders
    book_folder = os.path.join(out_dir, _slug_folder(title))
    _ensure_dir(book_folder)
    xhtml_dir = os.path.join(book_folder, "xhtml")
    _ensure_dir(xhtml_dir)

    items = []
    total_chapters = len(chapters)
    for idx, chap in enumerate(chapters, 1):
        chap_id = int(chap["id"])
        chap_slug = str(chap.get("slug") or "").strip()
        chap_title = str(chap.get("name") or f"Chương {idx}").strip()

        chapter_url = make_chapter_url(doc_base_url, chap_slug, chap_id)
        content_html = ""
        last_err = None
        for attempt in range(1, 4):
            try:
                content_html, _text = fetch_chapter_content(session, chapter_url, referer=doc_base_url)
                if content_html:
                    break
            except Exception as e:
                last_err = e
                print(f"  -> ❌ Lỗi tải chương (attempt {attempt}/3): {e}")
                time.sleep(1.0 * attempt)

        if not content_html:
            print(chapter_log_line(idx, total_chapters, "ERR", idx, total_chapters, f"{chap_title} ({last_err})"))
            continue

        xhtml = make_chapter_xhtml(chap_title, content_html)
        items.append({"title": chap_title, "xhtml_content": xhtml})

        # save debug xhtml
        fn = os.path.join(xhtml_dir, f"chap{idx:04d}.xhtml")
        with open(fn, "w", encoding="utf-8") as f:
            f.write(xhtml)

        print(chapter_log_line(idx, total_chapters, 200, idx, total_chapters, chap_title))
        time.sleep(sleep_between_chaps)

    if not items:
        raise RuntimeError("Không tải được chương nào để tạo EPUB.")

    print("\n--- BUILD EPUB ---")
    epub_path = create_epub_epub3_for_kobo(
        book_title=title,
        author=author,
        items=items,
        out_epub_dir=book_folder,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime,
        language=LANGUAGE,
        publisher=PUBLISHER,
    )
    #print("EPUB:", epub_path)
    return epub_path

# ========================= MENU / CLI =========================

def _print_banner():
    print("=" * 60)
    print("NTRUYEN DOWNLOADER -> BUILD EPUB (Hishiro tools)")
    print("=" * 60)

def run_menu():
    """
    Menu console (giống style script phongphongtam2_ver2).
    - Option 1: Tải truyện + tạo EPUB
    - Option 2: Lấy content 1 chương (doc-truyen)
    """
    _print_banner()
    while True:
        print("\n---------------- NTRUYEN MENU ----------------")
        print("\nChọn chức năng:")
        print("  1) Tải truyện + tạo EPUB")
        print("  2) Lấy content 1 chương (doc-truyen)")
        print("  0) Thoát")
        choice = input("Nhập lựa chọn: ").strip()

        if choice == "0":
            print("Bye!")
            return

        if choice == "1":
            url = input("Nhập URL truyện (https://ntruyen.biz/truyen/<slug>): ").strip()
            cover_in = input("Nhập cover (bỏ trống = auto | nhập đường dẫn file | nhập URL ảnh): ").strip()
            novel_id_in = input("novelId (Enter để auto, hoặc nhập khi bị 403): ").strip()
            novel_id = int(novel_id_in) if novel_id_in.isdigit() else None

            out_dir = input(f"Thư mục output (Enter = {OUTPUT_ROOT}): ").strip() or OUTPUT_ROOT
            limit_in = input(f"limit API (Enter = {API_LIMIT}): ").strip()
            limit = int(limit_in) if limit_in.isdigit() else API_LIMIT
            sort = input("sort asc/desc (Enter = asc): ").strip().lower() or API_SORT
            if sort not in ("asc", "desc"):
                sort = API_SORT

            try:
                epub_path = download_and_build_epub_ntruyen(
                    url,
                    novel_id=novel_id,
                    out_dir=out_dir,
                    cover_in=cover_in,
                    limit=limit,
                    sort=sort,
                )
                print(f"\n✅ DONE: {epub_path}")
            except Exception as e:
                print(f"\n❌ Lỗi: {e}")
            continue

        if choice == "2":
            chap_url = input("Nhập URL chương (https://ntruyen.biz/doc-truyen/...): ").strip()
            out_dir = input(f"Lưu debug vào thư mục (Enter = {OUTPUT_ROOT}): ").strip() or OUTPUT_ROOT
            try:
                session = _make_session()
                html_content, text_content = fetch_chapter_content(session, chap_url)
                if not html_content:
                    print("❌ Không lấy được nội dung chương (html_content rỗng).")
                    continue

                _ensure_dir(out_dir)
                safe_name = re.sub(r'[^0-9a-zA-Z_-]+', '_', chap_url.strip('/').split('/')[-1])[:120]
                html_path = os.path.join(out_dir, f"{safe_name}.html")
                txt_path = os.path.join(out_dir, f"{safe_name}.txt")
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html_content)
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(text_content)

                print("\n--- PREVIEW (TEXT) ---")
                print(text_content[:1500])
                if len(text_content) > 1500:
                    print("...")

                print(f"\n✅ Saved: {html_path}")
                print(f"✅ Saved: {txt_path}")
            except Exception as e:
                print(f"\n❌ Lỗi: {e}")
            continue

        print("Lựa chọn không hợp lệ. Vui lòng chọn lại.")

def main_cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Download ntruyen chapters and build EPUB.")
    ap.add_argument("url", help="Book url, ví dụ: https://ntruyen.biz/truyen/<slug>")
    ap.add_argument("--cover", default="", help="Cover: bỏ trống=auto | URL ảnh | đường dẫn file")
    ap.add_argument("--novel-id", type=int, default=None, help="novelId (dùng khi /truyen bị 403)")
    ap.add_argument("--out", default=OUTPUT_ROOT, help="Thư mục output")
    ap.add_argument("--limit", type=int, default=API_LIMIT, help="Số chương mỗi page API (default 50)")
    ap.add_argument("--sort", default=API_SORT, choices=["asc", "desc"], help="Sort chapters")
    args = ap.parse_args(argv)

    download_and_build_epub_ntruyen(
        args.url,
        novel_id=args.novel_id,
        out_dir=args.out,
        cover_in=args.cover,
        limit=args.limit,
        sort=args.sort,
    )

if __name__ == "__main__":
    import sys
    # Nếu có tham số URL -> chạy CLI; nếu không -> menu.
    if len(sys.argv) >= 2 and (sys.argv[1].startswith("http") or sys.argv[1].startswith("--")):
        # Cho phép ép menu bằng --menu
        if "--menu" in sys.argv:
            run_menu()
        else:
            main_cli()
    else:
        run_menu()
