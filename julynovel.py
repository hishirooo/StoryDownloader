# -*- coding: utf-8 -*-
from urllib.parse import urljoin
import requests, re, os, unicodedata, zipfile, io, time, html, datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from bs4 import BeautifulSoup, Comment
from download_logger import chapter_log_line
from epub_metadata import subject_xml

try:
    from PIL import Image
    HAS_PILLOW = True
except Exception:
    HAS_PILLOW = False

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 20
SLEEP_BETWEEN_CHAPS = 0.6
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)


def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


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


def _clean_chapter_title(raw_title: str, chapter_idx: int) -> str:
    cleaned_title = (raw_title or "").strip()
    cleaned_title = re.sub(r"\[[^\]]+\]", "", cleaned_title).strip()
    cleaned_title = re.sub(r"\s+", " ", cleaned_title).strip()

    m = re.search(r"(chương\s*[\d._-]+)", cleaned_title, re.IGNORECASE)
    if m:
        return m.group(1).strip().capitalize()

    return cleaned_title or f"Chương {chapter_idx}"


def _fetch_html(url: str, tries: int = 3, backoff: float = 0.8) -> BeautifulSoup:
    session = requests.Session()
    session.headers.update(HEADERS)

    for k in range(tries):
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff * (k + 1))
            continue
        if 400 <= r.status_code < 500:
            r.raise_for_status()
        r.raise_for_status()
        break

    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding

    return BeautifulSoup(r.text, "html.parser")


# =========================
# 1) BOOK INFO - JULYNOVEL
# =========================
def _get_book_info(soup: BeautifulSoup) -> dict:
    info = {
        "Title": "Unknown",
        "Author": "Unknown",
        "Genre": "N/A",
        "Status": "N/A",
        "CoverURL": "",
    }

    # title
    title_el = soup.select_one(".post-title h1") or soup.select_one("h1")
    info["Title"] = _text(title_el) or "Unknown"

    # author
    author_el = soup.select_one(".author-content a") or soup.select_one(".author-content")
    info["Author"] = _text(author_el) or "Unknown"

    # genres
    genre_els = soup.select(".genres-content a")
    if genre_els:
        info["Genre"] = ", ".join(_text(x) for x in genre_els if _text(x))

    # status
    for item in soup.select(".post-content_item"):
        heading = _text(item.select_one(".summary-heading"))
        content = _text(item.select_one(".summary-content"))
        if "Trạng thái" in heading:
            info["Status"] = content or "N/A"
            break

    # cover
    cover_el = soup.select_one(".summary_image img")
    if cover_el:
        info["CoverURL"] = (
            cover_el.get("src")
            or cover_el.get("data-src")
            or cover_el.get("srcset", "").split(" ")[0]
            or ""
        )

    return info


# =========================
# 2) LIST CHAPTERS - JULYNOVEL
# =========================
def _get_list_chapters(book_url: str) -> list:
    session = requests.Session()
    session.headers.update(HEADERS)

    # Cookie này khá quan trọng với site WP Manga có adult gate
    session.cookies.set("wpmanga-adault", "1")

    ajax_url = book_url.rstrip("/") + "/ajax/chapters/"

    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": book_url,
        "Origin": "https://julynovel.blog",
    }

    chapters = []

    try:
        r = session.post(ajax_url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()

        html_text = r.text.strip()
        if not html_text:
            print("⚠ AJAX chapters trả về rỗng.")
            return []

        soup = BeautifulSoup(html_text, "html.parser")

        for a in soup.select("li.wp-manga-chapter a"):
            href = a.get("href")
            title = _text(a)
            if href and title:
                chapters.append({
                    "title": title,
                    "url": href
                })

    except Exception as e:
        print(f"⚠ Lỗi lấy chapter bằng AJAX: {e}")
        return []

    def chapter_key(ch):
        url = ch.get("url", "")

        # Hỗ trợ:
        # /chuong-0/
        # /chuong-12/
        # /chuong-125_1/
        m = re.search(r"/chuong-([0-9]+(?:[_\.-][0-9]+)?)", url, re.IGNORECASE)
        if not m:
            return (10**9, url)

        raw = m.group(1).replace("_", ".").replace("-", ".")
        try:
            return (float(raw), url)
        except Exception:
            return (10**9, url)

    chapters = sorted(chapters, key=chapter_key)
    return chapters

# =========================
# 3) CHAPTER CONTENT - JULYNOVEL
# =========================
def _get_content_chapter(url: str) -> str:
    soup = _fetch_html(url)

    chapter_title = (
        _text(soup.select_one("#chapter-heading"))
        or _text(soup.select_one("h1"))
        or "Chương"
    )

    content_node = (
        soup.select_one(".reading-content")
        or soup.select_one(".text-left")
        or soup.select_one(".entry-content")
    )

    if not content_node:
        print(f"⚠ Không tìm thấy nội dung tại {url}")
        return ""

    # xóa script/style/noscript
    for tag in content_node.find_all(["script", "style", "noscript", "iframe"]):
        tag.decompose()

    # xóa comment html
    for c in content_node.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    # xóa p rỗng / credit ngắn / anti-copy text
    for p in list(content_node.find_all("p")):
        txt = p.get_text(" ", strip=True)
        if not txt:
            p.decompose()
            continue
        if "Sorry, you can't view or copy source codes this way!" in txt:
            p.decompose()

    # unwrap tag lạ
    allowed = {"p", "img", "br", "a", "strong", "b", "em", "i", "span", "u", "blockquote"}
    for tag in content_node.find_all():
        if tag.name not in allowed:
            try:
                tag.unwrap()
            except Exception:
                pass

    content_html = "\n".join(str(c) for c in content_node.children)
    content_html = re.sub(r'<\/?(html|head|body|title|doctype)[^>]*>', '', content_html, flags=re.I)

    xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi" lang="vi">
  <head>
    <meta charset="utf-8" />
    <title>{html.escape(chapter_title)}</title>
  </head>
  <body>
    <h1>{html.escape(chapter_title)}</h1>
    <section id="chapter" class="chapter-content">
{BeautifulSoup(content_html, "html.parser").prettify()}
    </section>
  </body>
</html>""".strip()

    return xhtml


def _get_content_chapters(chap_list: List[Dict[str, str]],
                          book_title: str | None = None,
                          base_output_dir: str | Path = "output") -> List[Tuple[int, str, str]]:
    if not chap_list:
        print("⚠ Không có chương nào trong chap_list.")
        return []

    book_dir_name = _slug_folder(book_title or "book")
    out_dir = ensure_dir(Path(base_output_dir) / book_dir_name)

    total = len(chap_list)
    results: List[Tuple[int, str, str]] = []
    print(f"\n--- Bắt đầu tải {total} chương ---")

    for i, chap in enumerate(chap_list, start=1):
        raw_title = chap.get("title") or f"Chương {i}"
        url = chap.get("url")
        try:
            xhtml = _get_content_chapter(url)
            if not xhtml:
                continue

            clean_title = _clean_chapter_title(raw_title, i)
            fname = f"{str(i).zfill(pad)}.xhtml"
            fpath = out_dir / fname

            with open(fpath, "w", encoding="utf-8") as f:
                f.write(xhtml)

            print(chapter_log_line(i, total, 200, i, total, clean_title))
            results.append((i, clean_title, xhtml))
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(chapter_log_line(i, total, "ERR", i, total, f"{raw_title} ({e})"))

    return results


# =========================
# 4) COVER HELPERS
# =========================
def _sniff_image_type(data: bytes):
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


def _ensure_jpeg_cover(img_bytes: bytes):
    if not img_bytes:
        return (None, None, None)

    if not HAS_PILLOW:
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)

    try:
        im = Image.open(io.BytesIO(img_bytes))
        resample_filter = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS

        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, resample_filter)

        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")

        out = io.BytesIO()
        im.save(out, format="JPEG", quality=90, optimize=True)
        return (out.getvalue(), ".jpg", "image/jpeg")
    except Exception as e:
        ext, mime = _sniff_image_type(img_bytes)
        print(f"⚠ Cover convert error: {e}. Dùng ảnh gốc {ext}.")
        return (img_bytes, ext, mime)


def _load_cover_from_input(cover_in: str, story_url: str, info_cover_url: str):
    data = None

    if cover_in:
        try:
            if cover_in.lower().startswith(("http://", "https://")):
                r = requests.get(cover_in, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.content
            else:
                with open(cover_in, "rb") as f:
                    data = f.read()
        except Exception as e:
            print(f"⚠ Không tải/đọc được cover '{cover_in}': {e}")

    if data is None and info_cover_url:
        try:
            print(f"Tự động lấy cover: {info_cover_url}")
            r = requests.get(info_cover_url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.content
        except Exception as e:
            print(f"⚠ Không tải được CoverURL mặc định: {e}")

    if data:
        return _ensure_jpeg_cover(data)

    return (None, None, None)


# =========================
# 5) EPUB
# =========================
def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    zipf.writestr(zinfo, data_bytes)


def create_epub_epub3_for_kobo(book_title: str, author: str, items: list,
                               out_epub_dir: str, cover_bytes=None, cover_ext=None, cover_mime=None,
                               language="vi", publisher="Hishiro", tags=None):
    os.makedirs(out_epub_dir, exist_ok=True)
    out_file = os.path.join(out_epub_dir, f"{_slugify_vi(book_title)}.epub")
    cover_rel = f"Images/cover{cover_ext}" if (cover_bytes and cover_ext) else None

    with zipfile.ZipFile(out_file, "w") as z:
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

        _epub_write(z, "OEBPS/Styles/style.css",
                    b"body{font-family:serif;line-height:1.6} img{max-width:100%;height:auto}")

        manifest_items = []
        spine_items = []
        toc_entries = []

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
            manifest_items.append('<item id="cover-html" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="cover-html" linear="no"/>')
            _epub_write(z, f"OEBPS/{cover_rel}", cover_bytes)

        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            chap_soup = BeautifulSoup(c.get("xhtml_content") or "", "html.parser")
            node = chap_soup.find("section", id="chapter") or chap_soup.find("body")
            h1_title = _text(chap_soup.find("h1"))
            content_html = "".join(str(x) for x in node.children) if node else ""

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
                f'  <h1>{html.escape(h1_title or c.get("title") or f"Chương {i}")}</h1>\n'
                f'  <div>{content_html}</div>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")

            _epub_write(z, f"OEBPS/{fn}", xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            toc_entries.append((fn, c.get("title") or f"Chương {i}"))

        nav_html = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" xmlns:epub="http://www.idpf.org/2007/ops">\n'
            '<head><meta charset="utf-8"/><title>TOC</title></head>\n'
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

        dt_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        subjects = subject_xml(tags, indent="    ")
        cover_item_line = (
            f'<item id="cover-img" href="{cover_rel}" media-type="{cover_mime or "image/jpeg"}" properties="cover-image"/>'
            if cover_rel else ""
        )
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
            f'{subjects}'
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


# =========================
# 6) MAIN FLOW
# =========================
def download_and_build_epub(url_story: str):
    soup = _fetch_html(url_story)
    info = _get_book_info(soup)

    title = info.get("Title") or "Truyện"
    author = info.get("Author") or "—"
    cover_auto_url = info.get("CoverURL") or ""

    print("--------------------------Thông tin truyện--------------------------")
    for k, v in info.items():
        print(f"{k}: {v}")

    chapters = _get_list_chapters(url_story)
    if not chapters:
        print("❌ Không tìm thấy chương nào.")
        return

    print(f"Total chapters: {len(chapters)}")

    cover_bytes, cover_ext, cover_mime = _load_cover_from_input("", url_story, cover_auto_url)
    print(f"Cover: {'OK' if cover_bytes else 'MISSING'} ({cover_mime or '-'})")

    out_dir = os.path.join("output", _slug_folder(title))
    os.makedirs(out_dir, exist_ok=True)

    chaps_saved = _get_content_chapters(chapters, book_title=title)

    all_items = []
    for idx, chap_title, xhtml in chaps_saved:
        all_items.append({"title": chap_title, "xhtml_content": xhtml})

    if not all_items:
        print("❌ Không có chương nào tải thành công.")
        return

    print("\n--- Bắt đầu đóng gói EPUB ---")
    epub_path = create_epub_epub3_for_kobo(
        book_title=title,
        author=author,
        items=all_items,
        out_epub_dir="output",
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime,
        language="vi",
        publisher="Hishiro",
        tags=info.get("Genre"),
    )
    print(f"✅ EPUB: {epub_path}")
    print(f"📁 Thư mục chương đã lưu: {out_dir}")


if __name__ == "__main__":
    url = "https://luclacnho2810.wordpress.com/bo-tat-dien-cuong-qua/"
    download_and_build_epub(url)
