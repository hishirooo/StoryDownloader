# -*- coding: utf-8 -*-
import os, re, time, html, unicodedata, datetime, zipfile
from pathlib import Path
from typing import Optional, List, Dict, Tuple
import requests
from bs4 import BeautifulSoup, Tag, NavigableString

# =============== CẤU HÌNH ===============
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)  # (w, h) Kobo-friendly

# =============== PILLOW (tuỳ chọn) ===============
try:
    from PIL import Image
    HAS_PILLOW = True
except Exception:
    HAS_PILLOW = False
    os.system("pip install Pillow")
    from PIL import Image

# =============== TIỆN ÍCH CHUNG ===============
def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""

def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    for k in range(tries):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff*(k+1)); continue
        if 400 <= r.status_code < 500:
            r.raise_for_status()
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")
    # nếu tới đây vẫn không return:
    raise RuntimeError("Không tải được HTML.")

def ensure_dir(path: str | Path) -> Path:
    p = Path(path); p.mkdir(parents=True, exist_ok=True); return p

def sanitize_filename(name: str, repl: str = "_") -> str:
    name = (name or "").strip()
    name = re.sub(r"[\\/:*?\"<>|]+", repl, name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "book"

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

# =============== LẤY THÔNG TIN TRUYỆN (bububaoboi) ===============
def get_book_info(book_url: str) -> Dict[str, str]:
    """
    Lấy thông tin truyện từ bububaoboi.com.
    Trả về dict: Title, Author, CoverURL (bạn đã có sẵn trong bản của bạn).
    """
    soup = _fetch_html(book_url)
    with open("debug_bububaoboi.html", "w", encoding="utf-8") as f:
        f.write(str(soup))

    info = {}
    title_node = soup.find("div", class_="col large-8")
    info["Title"] = _text(title_node.find("h1") if title_node else None)

    info_node = soup.find("div", class_="mta_ngan")
    # Author
    author_el = info_node.find("strong", string=re.compile(r"Tác giả:")).parent if info_node else None
    info["Author"] = _text(author_el)

    # CoverURL
    cover_el = soup.find("img", class_="wp-post-image")
    info["CoverURL"] = cover_el["src"] if cover_el and cover_el.get("src") else ""

    return info

def _get_list_chapter(soup: BeautifulSoup) -> List[Dict[str, str]]:
    chap_list = []
    chap_container = soup.find("div", class_="chapter-list")
    if not chap_container:
        return chap_list
    for a in chap_container.find_all("a", href=True):
        chap_list.append({"title": _text(a), "url": a["href"]})
    return chap_list

# =============== BÓC NỘI DUNG CHƯƠNG (HTML sạch) ===============
def _get_content_chapter(url: str) -> str:
    """
    Trả về chuỗi XHTML gọn, hợp lệ để nhét thẳng vào EPUB (một trang .xhtml).
    - Bóc <div class="chapter-content">
    - Bỏ toàn bộ attributes (style, data-*)
    - Unwrap toàn bộ <span>
    - Dọn <p> rỗng, chuẩn hoá <br/>
    """
    soup = _fetch_html(url)

    with open("debug_bububaoboi_chap.html", "w", encoding="utf-8") as f:
        f.write(str(soup))

    chapter_title = _text(soup.find("h1", class_="chapter-title")) or "Chương"
    #print(f"Chapter title: {chapter_title}")

    content_node = soup.find("div", class_="chapter-content")
    if not content_node:
        return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi" lang="vi">
  <head><meta charset="utf-8"/><title>{chapter_title}</title></head>
  <body><section id="chapter" class="chapter-content"></section></body>
</html>"""

    with open("debug_bububaoboi_content_chapter.html", "w", encoding="utf-8") as f:
        f.write(str(content_node))

    # 1) Bỏ mọi attribute (style, data-*, class…)
    for tag in content_node.find_all(True):
        tag.attrs = {}

    # 2) Unwrap span
    for sp in content_node.find_all("span"):
        sp.unwrap()

    # 3) Xoá <p> rỗng
    for p in list(content_node.find_all("p")):
        txt = p.get_text(strip=True)
        only_br = all(
            (isinstance(ch, Tag) and ch.name == "br") or
            (isinstance(ch, NavigableString) and str(ch).strip() == "")
            for ch in p.children
        )
        if txt == "" and only_br:
            p.decompose()

    # 4) Chuẩn hoá <br> liên tiếp (>2 -> 2)
    html_str = str(content_node)
    html_str = re.sub(r"(?:<br/?>\s*){3,}", "<br/>\n<br/>", html_str, flags=re.I)
    html_str = re.sub(r"[ \t]+\n", "\n", html_str)

    # 5) Gói vào XHTML hợp lệ
    xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi" lang="vi">
  <head>
    <meta charset="utf-8" />
    <title>{chapter_title}</title>
  </head>
  <body>
    <h1>{chapter_title}</h1>
    <section id="chapter" class="chapter-content">
{BeautifulSoup(html_str, "html.parser").prettify()}
    </section>
  </body>
</html>""".strip()

    return xhtml

# =============== GET TOÀN BỘ CHƯƠNG & LƯU FILE ===============
def _get_content_chapters(chap_list: List[Dict[str, str]],
                          book_title: str | None = None,
                          base_output_dir: str | Path = "output") -> List[Tuple[int, str, str]]:
    """
    Lấy nội dung tất cả chương trong chap_list.
    - Ghi file vào: output/<tên_truyện>/<idx:03>.xhtml
    - In log: [xxx/yyy] Saved - <title> - <path>
    - Trả về: List[ (idx, title, xhtml_content) ]
    """
    if not chap_list:
        print("⚠ Không có chương nào trong chap_list.")
        return []

    book_dir_name = sanitize_filename(book_title or "book")
    out_dir = ensure_dir(Path(base_output_dir) / book_dir_name)

    total = len(chap_list)
    results: List[Tuple[int, str, str]] = []
    pad = max(3, len(str(total)))

    for i, chap in enumerate(chap_list, start=1):
        title = chap.get("title") or f"Chương {i}"
        url   = chap.get("url")
        try:
            xhtml = _get_content_chapter(url)
            fname = f"{str(i).zfill(pad)}.xhtml"
            fpath = out_dir / fname
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(xhtml)
            print(f"[{str(i).zfill(pad)}/{str(total).zfill(pad)}] Saved - {title} - {fpath}")
            results.append((i, title.strip(), xhtml))
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"[{str(i).zfill(pad)}/{str(total).zfill(pad)}] ❌ Lỗi lấy '{title}' ({url}): {e}")
    return results

# =============== COVER HELPERS ===============
def _sniff_image_type(data: bytes):
    if not data or len(data) < 12: return (".bin", "application/octet-stream")
    if data[:3] == b"\xff\xd8\xff": return (".jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return (".png", "image/png")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return (".webp", "image/webp")
    if data[:6] in (b"GIF87a", b"GIF89a"): return (".gif", "image/gif")
    return (".bin", "application/octet-stream")

def _ensure_jpeg_cover(img_bytes: bytes):
    if not img_bytes: return (None, None, None)
    if not HAS_PILLOW:
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)
    try:
        import io as _io
        im = Image.open(_io.BytesIO(img_bytes))
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        out = _io.BytesIO()
        im.save(out, format="JPEG", quality=90, optimize=True)
        return (out.getvalue(), ".jpg", "image/jpeg")
    except Exception as e:
        ext, mime = _sniff_image_type(img_bytes)
        print(f"⚠ Cover convert error: {e}. Dùng ảnh gốc {ext}.")
        return (img_bytes, ext, mime)

def _fetch_cover_from_book_page_bububaoboi(book_url: str):
    soup = _fetch_html(book_url)
    img = soup.find("img", class_="wp-post-image")
    if img and img.get("src"):
        src = img["src"]
        r = requests.get(src, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content
    return None

def _load_cover_from_input(cover_in: str, story_url: str, info_cover_url: str):
    """
    cover_in:
      - rỗng  -> auto dùng info_cover_url (get_book_info) hoặc lấy trên trang
      - http… -> tải URL ảnh (mode 2)
      - path  -> đọc ảnh máy (mode 1)
    Trả: (bytes, ext, mime) đã cố convert JPEG/resize.
    """
    data = None
    if cover_in:
        try:
            if cover_in.lower().startswith(("http://", "https://")):
                print(f"Tải cover từ URL: {cover_in}")
                r = requests.get(cover_in, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.content
            else:
                print(f"Đọc cover từ file: {cover_in}")
                with open(cover_in, "rb") as f:
                    data = f.read()
        except Exception as e:
            print(f"⚠ Không tải/đọc được cover '{cover_in}': {e}")

    if data is None:
        # Auto: ưu tiên info["CoverURL"], fallback lấy lại trên trang
        if info_cover_url:
            try:
                print(f"Tự động lấy cover từ CoverURL trong get_book_info: {info_cover_url}")
                r = requests.get(info_cover_url, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.content
            except Exception as e:
                print(f"⚠ Không tải được CoverURL mặc định: {e}. Thử lấy trực tiếp trên trang…")
        if data is None:
            data = _fetch_cover_from_book_page_bububaoboi(story_url)

    if data:
        return _ensure_jpeg_cover(data)
    return (None, None, None)

# =============== EPUB3 (Kobo-friendly) ===============
def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    zipf.writestr(zinfo, data_bytes)

def create_epub_epub3_for_kobo(book_title: str, author: str, items: list,
                               out_epub_dir: str, cover_bytes=None, cover_ext=None, cover_mime=None,
                               language="vi", publisher="Hishiro"):
    os.makedirs(out_epub_dir, exist_ok=True)
    out_file = os.path.join(out_epub_dir, f"{_slugify_vi(book_title)}.epub")
    cover_rel = f"Images/cover{cover_ext}" if (cover_bytes and cover_ext) else None

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

        # 2) CSS
        _epub_write(z, "OEBPS/Styles/style.css", b"body{font-family:serif;line-height:1.6} img{max-width:100%;height:auto}")

        # 3) Cover page
        if cover_rel:
            cover_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi">\n'
                '<head><title>Cover</title><meta charset="utf-8"/></head>\n'
                '<body>\n'
                f'  <img src="../{cover_rel}" alt="cover" style="max-width:100%;height:auto;display:block;margin:0 auto;"/>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/Text/cover.xhtml", cover_xhtml)

        # 4) Chapters
        manifest_items = []
        spine_items    = []
        toc_entries    = []
        if cover_rel:
            manifest_items.append('<item id="cover-html" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="cover-html" linear="no"/>')

        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">\n'
                '<head>\n'
                f'  <title>{html.escape(c.get("title") or f"Chương {i}")}</title>\n'
                '  <meta charset="utf-8"/>\n'
                '  <link href="../Styles/style.css" rel="stylesheet" type="text/css"/>\n'
                '</head>\n'
                '<body>\n'
                f'  <h1>{html.escape(c.get("title") or f"Chương {i}")}</h1>\n'
                f'  <div>{c.get("content_html") or ""}</div>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, f"OEBPS/{fn}", xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            toc_entries.append((fn, c.get("title") or f"Chương {i}"))

        # 5) Cover image
        cover_item_line = ""
        if cover_rel and cover_bytes:
            _epub_write(z, f"OEBPS/{cover_rel}", cover_bytes)
            cover_item_line = f'<item id="cover-img" href="{cover_rel}" media-type="{cover_mime or "image/jpeg"}" properties="cover-image"/>'

        # 6) nav.xhtml
        nav_html = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" xmlns:epub="http://www.idpf.org/2007/ops">\n'
            '<head>\n'
            '  <meta charset="utf-8"/>\n'
            '  <title>Table of Contents</title>\n'
            '  <link href="Styles/style.css" rel="stylesheet" type="text/css"/>\n'
            '</head>\n'
            '<body>\n'
            '  <nav epub:type="toc" id="toc">\n'
            f'    <h1>{html.escape(book_title)}</h1>\n'
            '    <ol>\n' +
            "\n".join(f'      <li><a href="{fn}">{html.escape(t)}</a></li>' for fn, t in toc_entries) +
            '\n    </ol>\n'
            '  </nav>\n'
            '</body>\n'
            '</html>'
        ).encode("utf-8")
        _epub_write(z, "OEBPS/nav.xhtml", nav_html)

        # 7) content.opf (EPUB3, publisher = Hishiro)
        dt_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest_str = "\n    ".join([
            '<item id="css" href="Styles/style.css" media-type="text/css"/>',
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            *manifest_items,
            *([cover_item_line] if cover_item_line else [])
        ])
        spine_str = "\n    ".join(spine_items)
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="3.0">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'    <dc:identifier id="BookID">urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}</dc:identifier>\n'
            f'    <dc:title>{html.escape(book_title)}</dc:title>\n'
            f'    <dc:creator>{html.escape(author or "—")}</dc:creator>\n'
            f'    <dc:publisher>{html.escape(publisher)}</dc:publisher>\n'
            f'    <dc:language>{language}</dc:language>\n'
            f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
            '  </metadata>\n'
            '  <manifest>\n'
            f'    {manifest_str}\n'
            '  </manifest>\n'
            '  <spine>\n'
            f'    {spine_str}\n'
            '  </spine>\n'
            '</package>'
        ).encode("utf-8")
        _epub_write(z, "OEBPS/content.opf", opf)

    return out_file

# =============== MAIN FLOW ===============
def main():
    story_url = input("Nhập URL truyện (bububaoboi): ").strip()
    cover_in  = input("Nhập cover (bỏ trống = auto | nhập đường dẫn file | nhập URL ảnh): ").strip()

    # 1) Info
    info = get_book_info(story_url)
    title  = info.get("Title") or "Truyện"
    author = info.get("Author") or "—"
    cover_auto_url = info.get("CoverURL") or ""

    print("--------------------------Thông tin truyện--------------------------")
    for k, v in info.items():
        print(f"{k}: {v}")

    # 2) Danh sách chương
    soup = _fetch_html(story_url)
    chapters = _get_list_chapter(soup)
    print(f"Total chapters: {len(chapters)}")

    # 3) Cover (3 chế độ: local path | URL | auto từ get_book_info/ trang)
    cover_bytes, cover_ext, cover_mime = _load_cover_from_input(cover_in, story_url, cover_auto_url)
    print(f"Cover: {'OK' if cover_bytes else 'MISSING'} ({cover_mime or '-'})")

    # 4) Lưu chương XHTML sạch vào output/TênTruyện
    out_dir = os.path.join("output", _slug_folder(title))
    os.makedirs(out_dir, exist_ok=True)

    chaps_saved = _get_content_chapters(chapters, book_title=title)  # [(idx, title, xhtml)]

    # Gom item để build EPUB3
    all_items = []
    for idx, chap_title, xhtml in chaps_saved:
        soup_x = BeautifulSoup(xhtml, "html.parser")
        node = soup_x.find("section") or soup_x.body or soup_x
        content_html = "".join(str(c) for c in node.children)
        all_items.append({"title": chap_title, "content_html": content_html})

    # 5) Build EPUB3 (publisher = Hishiro) -> output/
    epub_path = create_epub_epub3_for_kobo(
        book_title=title,
        author=author,
        items=all_items,
        out_epub_dir="output",
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime,
        language="vi",
        publisher="Hishiro"
    )
    print(f"✅ EPUB: {epub_path}")
    print(f"📁 Thư mục chương: {out_dir}")

if __name__ == "__main__":
    main()
