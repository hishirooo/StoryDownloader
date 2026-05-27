# -*- coding: utf-8 -*-
"""
monkeydtruyen_simple.py
Chỉ cần nhập URL truyện:
- Tự tải toàn bộ chương thành HTML
- Tự lấy cover từ web
- Tự build EPUB2
"""

from bs4 import BeautifulSoup
from typing import Optional, List, Dict
from urllib.parse import urljoin
import requests, re, html, os, unicodedata, zipfile, time, datetime as dt
from download_logger import chapter_log_line
from epub_metadata import PUBLISHER, subject_xml

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 20
SLEEP_BETWEEN_CHAPS = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_COVER_SIZE = (1600, 2400)

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("⚠ Không tìm thấy Pillow. Cover sẽ dùng ảnh gốc nếu không convert được.")


def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""


def _safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:150] or "chapter"


def _slugify_vi(s: str) -> str:
    s = (s or "book").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-_")
    return (s or "book")[:80]


def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    for k in range(tries):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff * (k + 1))
            continue
        if 400 <= r.status_code < 500:
            r.raise_for_status()
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        soup.base_url = url
        return soup
    return BeautifulSoup("", "html.parser")


_NOISE_RE = re.compile(
    r"(Mời\s+Quý\s+độc\s+giả|CLICK\b|mở\s+ứng\s+dụng\s+Shopee|s\.shopee\.vn|đọc\s+toàn\s+bộ\s+chương)",
    re.I
)


def _is_noise_paragraph(t: str) -> bool:
    t = (t or "").strip()
    if not t:
        return True
    if _NOISE_RE.search(t):
        return True
    if len(t) <= 2 and all(ch in ".•*·-—–" or ch.isspace() for ch in t):
        return True
    return False


def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    info = {"title": "", "author": "", "genre": "", "status": ""}
    try:
        title = _text(soup.find("h2", class_="card-title")) or _text(soup.title)

        dts = soup.select("div.card-body dl.row dt")
        dds = soup.select("div.card-body dl.row dd")
        label_to_val = {}
        for i, dt_node in enumerate(dts):
            if i < len(dds):
                label_to_val[_text(dt_node)] = _text(dds[i])

        author = label_to_val.get("Tác giả", "N/A")

        genres = "N/A"
        idx = [i for i, dt_node in enumerate(dts) if _text(dt_node) == "Thể loại"]
        if idx:
            gdd = dds[idx[0]]
            tags = [a.get_text(strip=True) for a in gdd.select("a")]
            genres = " - ".join(tags) if tags else _text(gdd)

        status = label_to_val.get("Trạng thái", "N/A")

        info["title"] = title
        info["author"] = author
        info["genre"] = genres
        info["status"] = status
    except Exception:
        pass
    return info


def _get_cover_link(soup: BeautifulSoup) -> str:
    try:
        n = soup.select_one("img.img-fluid")
        return n.get("src") if n and n.get("src") else ""
    except Exception:
        return ""


def _get_list_chapters(soup: BeautifulSoup) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    try:
        for a in soup.select("div.list-chapters div.episode-title a[href]"):
            title = _text(a)
            href = a.get("href")
            if not title or not href:
                continue
            if not href.startswith("http"):
                href = urljoin(getattr(soup, "base_url", ""), href)
            chapters.append({"title": title, "url": href})
    except Exception:
        pass
    return list(reversed(chapters))


_IMPORT_RE = re.compile(r'@import\s+(?:url\()?["\']?([^"\')]+)["\']?\)?\s*;', re.I)


def _css_decode_content(s: str) -> str:
    s = s.strip()
    s = s.replace(r"\A", "\n").replace(r"\a", "\n")

    def repl_hex(m):
        try:
            return chr(int(m.group(1), 16))
        except Exception:
            return m.group(0)

    s = re.sub(r"\\([0-9a-fA-F]{1,6})\s?", repl_hex, s)
    s = s.replace(r"\'", "'").replace(r"\"", '"').replace(r"\\", "\\")
    return s


def _collect_css_texts(soup: BeautifulSoup, base_url: str) -> List[str]:
    css_texts: List[str] = []

    for st in soup.find_all("style"):
        if st.string:
            css_texts.append(st.string)

    for link in soup.find_all("link"):
        rel = [x.lower() for x in (link.get("rel") or [])]
        as_attr = (link.get("as") or "").lower()
        href = link.get("href")
        if not href:
            continue

        take = False
        if any("stylesheet" in x for x in rel):
            take = True
        if ("preload" in rel and as_attr == "style"):
            take = True
        if href.endswith(".css"):
            take = True
        if not take:
            continue

        css_url = urljoin(base_url, href)
        try:
            r = requests.get(css_url, headers=HEADERS, timeout=30)
            if r.ok:
                text = r.text
                css_texts.append(text)
                for imp in _IMPORT_RE.findall(text):
                    imp_url = urljoin(css_url, imp)
                    try:
                        r2 = requests.get(imp_url, headers=HEADERS, timeout=30)
                        if r2.ok:
                            css_texts.append(r2.text)
                    except requests.RequestException:
                        pass
        except requests.RequestException:
            pass

    return css_texts


def _build_span_map_from_css(css_texts: List[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    block_re = re.compile(r'(?P<selectors>[^{]+){(?P<body>[^{}]*content\s*:[^;]+;[^}]*)}', re.S)
    str_token_re = re.compile(r'("([^"]*)"|\'([^\']*)\')')

    for css in css_texts:
        for blk in block_re.finditer(css):
            selectors = blk.group("selectors")
            body = blk.group("body")
            sel_list = [s.strip() for s in selectors.split(",")]
            sel_classes = []

            for s in sel_list:
                m = re.search(r'\.([A-Za-z0-9_-]+)\s*::?be?fore\b', s)
                if not m:
                    m = re.search(r'\.([A-Za-z0-9_-]+)\s*::?after\b', s)
                if m:
                    sel_classes.append(m.group(1))

            if not sel_classes:
                continue

            joined = ""
            for sm in str_token_re.finditer(body):
                piece = sm.group(2) if sm.group(2) is not None else sm.group(3)
                joined += _css_decode_content(piece)

            if not joined:
                continue

            for cls in sel_classes:
                if cls not in mapping:
                    mapping[cls] = joined

    return mapping


def _replace_spans_with_text(root: BeautifulSoup, cls_map: Dict[str, str]):
    container = root.select_one("div#chapter-content-render")
    if not container:
        return

    for sp in container.find_all("span"):
        classes = sp.get("class") or []
        buf = []
        for c in classes:
            t = cls_map.get(c)
            if t:
                buf.append(t)
        if buf:
            sp.replace_with("".join(buf))


def _pick_chapter_title(soup: BeautifulSoup) -> Optional[str]:
    for sel in ["h1.card-title", "h1.title", "h1", "h2.title", "h2"]:
        n = soup.select_one(sel)
        if n and _text(n):
            return _text(n)
    h = soup.find(["h1", "h2", "h3"])
    return _text(h) if h else None


def fetch_chapter_content(url: str) -> Dict[str, str]:
    soup = _fetch_html(url)

    css_texts = _collect_css_texts(soup, url)
    cls_map = _build_span_map_from_css(css_texts)
    _replace_spans_with_text(soup, cls_map)

    title = _pick_chapter_title(soup) or "Chương"
    container = soup.select_one("div#chapter-content-render")
    if not container:
        divs = sorted(soup.find_all("div"), key=lambda d: len(d.get_text(" ", strip=True)), reverse=True)
        container = divs[0] if divs else soup

    parts: List[str] = []
    ps = container.find_all("p")
    if ps:
        for p in ps:
            for br in p.find_all("br"):
                br.replace_with("\n")
            t = p.get_text()
            t = t.replace("\xa0", " ")
            t = re.sub(r"[ \t]+\n", "\n", t)
            t = re.sub(r"\n[ \t]+", "\n", t)
            t = re.sub(r" {2,}", " ", t)
            if _is_noise_paragraph(t):
                continue
            safe = html.escape(t.strip()).replace("\n", "<br/>")
            parts.append(f"<p>{safe}</p>")
        content_html = "\n".join(parts) if parts else "<p>(Trống)</p>"
    else:
        content_html = "<p>(Trống)</p>"

    return {"title": title, "content_html": content_html, "url": url}


HTML_TEMPLATE = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{doc_title}</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.7;max-width:820px;margin:2rem auto;padding:0 1rem;background:#f4f4f6;color:#222}}
    h1{{font-size:1.6rem;margin:0 0 1rem}}
    .meta{{color:#666;font-size:.9rem;margin-bottom:1rem}}
    img{{max-width:100%;height:auto}}
    p{{margin:.6rem 0}}
    article{{background:#fff;border-radius:12px;padding:1rem 1.2rem;box-shadow:0 1px 10px rgba(0,0,0,.06)}}
  </style>
</head>
<body>
  <h1>{chapter_title}</h1>
  <div class="meta">{book_title} · <a href="{src}">Nguồn</a></div>
  <article>
  {content}
  </article>
</body>
</html>"""


def save_chapter_html(book_title: str, chapter_idx: int, chap: dict, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{chapter_idx:04d} - {_safe_filename(chap.get('title') or f'Chuong {chapter_idx}')}.html"
    path = os.path.join(out_dir, fname)
    html_out = HTML_TEMPLATE.format(
        doc_title=f"{book_title} - {chap.get('title') or f'Chương {chapter_idx}'}",
        chapter_title=html.escape(chap.get('title') or f"Chương {chapter_idx}"),
        book_title=html.escape(book_title or "Truyện"),
        src=chap.get("url") or "",
        content=chap.get("content_html") or "<p>(Không có nội dung)</p>",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_out)
    return path


def save_all_chapters_to_html(book_title: str, chapters: list, out_dir: str) -> List[str]:
    n = len(chapters)
    saved: List[str] = []

    for i, info in enumerate(chapters, start=1):
        try:
            chap = fetch_chapter_content(info["url"])
            if not chap.get("title"):
                chap["title"] = info.get("title")
            p = save_chapter_html(book_title, i, chap, out_dir)
            print(chapter_log_line(i, n, chap.get("status_code", 200), i, n, chap.get("title") or info.get("title") or ""), flush=True)
            saved.append(p)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(chapter_log_line(i, n, "ERR", i, n, f"{info.get('title') or info.get('url')} ({e})"), flush=True)

    return saved


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
    if not HAS_PILLOW:
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)

    try:
        im = Image.open(__import__("io").BytesIO(img_bytes))
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)

        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")

        out = __import__("io").BytesIO()
        im.save(out, format="JPEG", quality=90, optimize=True)
        return (out.getvalue(), ".jpg", "image/jpeg")
    except Exception:
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)


def _fetch_cover_from_book_page(book_page_url: str):
    soup = _fetch_html(book_page_url)
    src = _get_cover_link(soup)
    if src:
        src = urljoin(book_page_url, src)
        r = requests.get(src, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content
    return None


def _load_cover_from_web(story_url: str):
    try:
        print("Tự lấy cover từ trang truyện...")
        data = _fetch_cover_from_book_page(story_url)
        if data:
            return _ensure_jpeg_cover(data)
    except Exception as e:
        print(f"⚠ Không lấy được cover: {e}")
    return (None, None, None)


def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)


def build_epub2_from_htmls(
    html_paths: List[str],
    book_title: str,
    author: str,
    out_dir: str,
    cover_bytes: Optional[bytes] = None,
    cover_ext: Optional[str] = None,
    cover_mime: Optional[str] = None,
    genre: Optional[str] = None,
) -> str:
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"{_slugify_vi(book_title)}.epub")

    items = []
    for html_path in html_paths:
        with open(html_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        title_node = soup.find("h1") or soup.find("title")
        title = title_node.get_text(strip=True) if title_node else os.path.basename(html_path)
        node = soup.select_one("article") or soup.body or soup
        content_html = "".join(str(x) for x in node.children)
        items.append({"title": title, "content_html": content_html})

    book_id = f"urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}"
    cover_href = f"images/cover{cover_ext}" if cover_bytes and cover_ext else None
    subjects = subject_xml(genre, indent="    ")

    with zipfile.ZipFile(out_file, "w") as z:
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""".encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)

        css = b"""body{font-family:serif;line-height:1.6;margin:5%;} h1{text-align:center;} img{max-width:100%;height:auto;}"""
        _epub_write(z, "OEBPS/style.css", css)

        manifest_items = [
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '<item id="style" href="style.css" media-type="text/css"/>',
        ]
        spine_items = []
        nav_points = []

        if cover_href:
            z.writestr(
                "OEBPS/cover.xhtml",
                f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Cover</title></head>
<body><div><img src="{cover_href}" alt="cover"/></div></body>
</html>""".encode("utf-8"),
            )
            _epub_write(z, f"OEBPS/{cover_href}", cover_bytes)
            manifest_items.append('<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>')
            manifest_items.append(f'<item id="cover-image" href="{cover_href}" media-type="{cover_mime or "image/jpeg"}"/>')
            spine_items.append('<itemref idref="cover" linear="yes"/>')

        for i, item in enumerate(items, start=1):
            chap_name = f"chap_{i:04d}.xhtml"
            chap_id = f"chap{i}"
            chap_html = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{html.escape(item["title"])}</title>
  <link href="style.css" rel="stylesheet" type="text/css"/>
</head>
<body>
  <h1>{html.escape(item["title"])}</h1>
  {item["content_html"]}
</body>
</html>"""
            _epub_write(z, f"OEBPS/{chap_name}", chap_html.encode("utf-8"))
            manifest_items.append(f'<item id="{chap_id}" href="{chap_name}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="{chap_id}"/>')
            nav_points.append((i, chap_name, item["title"]))

        ncx_points = []
        play_order = 1
        if cover_href:
            ncx_points.append(f"""
    <navPoint id="nav-cover" playOrder="{play_order}">
      <navLabel><text>Cover</text></navLabel>
      <content src="cover.xhtml"/>
    </navPoint>""")
            play_order += 1

        for _, chap_name, chap_title in nav_points:
            ncx_points.append(f"""
    <navPoint id="nav-{play_order}" playOrder="{play_order}">
      <navLabel><text>{html.escape(chap_title)}</text></navLabel>
      <content src="{chap_name}"/>
    </navPoint>""")
            play_order += 1

        toc_ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{book_id}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(book_title)}</text></docTitle>
  <navMap>
    {''.join(ncx_points)}
  </navMap>
</ncx>"""
        _epub_write(z, "OEBPS/toc.ncx", toc_ncx.encode("utf-8"))

        metadata = f"""
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{html.escape(book_title)}</dc:title>
    <dc:creator>{html.escape(author or "—")}</dc:creator>
    <dc:language>vi</dc:language>
    <dc:identifier id="BookId">{book_id}</dc:identifier>
    <dc:date>{dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}</dc:date>
    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>
{subjects.rstrip()}
    <meta name="cover" content="cover-image"/>
  </metadata>""" if cover_href else f"""
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{html.escape(book_title)}</dc:title>
    <dc:creator>{html.escape(author or "—")}</dc:creator>
    <dc:language>vi</dc:language>
    <dc:identifier id="BookId">{book_id}</dc:identifier>
    <dc:date>{dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}</dc:date>
    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>
{subjects.rstrip()}
  </metadata>"""

        opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId" version="2.0">
{metadata}
  <manifest>
    {''.join(manifest_items)}
  </manifest>
  <spine toc="ncx">
    {''.join(spine_items)}
  </spine>
</package>"""
        _epub_write(z, "OEBPS/content.opf", opf.encode("utf-8"))

    return out_file


def download_html_and_build_epub2(story_url: str):
    soup = _fetch_html(story_url)
    info = _get_book_info(soup)

    title = info.get("title") or "Truyện"
    author = info.get("author") or "—"
    genre = info.get("genre") or "N/A"
    status = info.get("status") or "N/A"

    print("----------------- THÔNG TIN TRUYỆN -----------------")
    print(f"Title : {title}")
    print(f"Author: {author}")
    print(f"Genre : {genre}")
    print(f"Status: {status}")
    print("----------------------------------------------------")

    chapters = _get_list_chapters(soup)
    if not chapters:
        raise ValueError("Không tìm thấy danh sách chương.")

    print(f"Total chapters: {len(chapters)}")

    cover_bytes, cover_ext, cover_mime = _load_cover_from_web(story_url)
    print(f"Cover status: {'OK' if cover_bytes else 'MISSING'} ({cover_mime or '-'})")

    book_slug = _slugify_vi(title)
    html_out_dir = os.path.join("output", book_slug)
    epub_out_dir = "output"

    os.makedirs(html_out_dir, exist_ok=True)
    os.makedirs(epub_out_dir, exist_ok=True)

    print("\n[1/2] Tải HTML...")
    html_paths = save_all_chapters_to_html(title, chapters, html_out_dir)
    if not html_paths:
        raise ValueError("Không tải được HTML chương nào.")

    print(f"✔ Đã lưu HTML: {len(html_paths)} file(s)")

    print("\n[2/2] Build EPUB2...")
    epub_path = build_epub2_from_htmls(
        html_paths=html_paths,
        book_title=title,
        author=author,
        out_dir=epub_out_dir,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime,
        genre=genre,
    )

    print("\n------------------- DONE -------------------")
    print(f"✅ EPUB: {epub_path}")
    print(f"✅ HTML: {html_out_dir}")


def main():
    story_url = input("Nhập URL truyện: ").strip()
    if not story_url:
        print("❌ URL trống.")
        return

    try:
        download_html_and_build_epub2(story_url)
    except Exception as e:
        print(f"❌ Lỗi: {e}")



try:
    from download_policy import install_adapter_policy as _install_adapter_policy
    _install_adapter_policy(globals())
except Exception:
    pass
if __name__ == "__main__":
    main()
