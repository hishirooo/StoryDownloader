# -*- coding: utf-8 -*-
"""
shiningjustforme_wp.py
---------------------------------
Module/menu tải truyện từ shiningjustforme.wordpress.com và tạo EPUB.
Thiết kế theo layout quen thuộc:
[1] Tải & lưu HTML
[2] Tải & lưu TXT (trích từ HTML; nếu chưa có HTML sẽ tải trước)
[3] Tải & lưu HTML + TXT
[4] Tải & lưu HTML + Build EPUB
[5] Tải & lưu TXT + Build EPUB (sẽ tải mới nếu cần)
[6] Tải & lưu HTML + TXT + Build EPUB

Có thể chọn khoảng chương (start/end), resume, chọn EPUB2/EPUB3.
Tương thích Kobo (có cover.xhtml).

Cách chạy nhanh:
    python shiningjustforme_wp.py
"""

from __future__ import annotations

import os, re, sys, time, html, unicodedata, zipfile
import io as _io
import datetime as dt
from typing import List, Tuple, Dict, Optional

import requests
from bs4 import BeautifulSoup, Tag

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 25
SLEEP_BETWEEN_CHAPS = 0.20
RETRY_STATUS = {429, 500, 502, 503, 504}

DEFAULT_OUT = "Output"
MAX_COVER_SIZE = (1600, 2400)  # (W,H)

# Pillow (tùy chọn cho convert/resize cover)
try:
    from PIL import Image
    HAS_PILLOW = True
except Exception:
    HAS_PILLOW = False

# ================== Helpers chung ====================
def _slugify_vi(s: str, allow_unicode: bool = False) -> str:
    s = s.strip()
    if not allow_unicode:
        s = unicodedata.normalize('NFKD', s)
        s = s.encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r"[^\w\s-]", "", s.lower(), flags=re.U)
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s or "untitled"

def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""

class Http:
    def __init__(self):
        self.sess = requests.Session()
        adapter = requests.adapters.HTTPAdapter(max_retries=3)
        self.sess.mount("http://", adapter)
        self.sess.mount("https://", adapter)

    def get_html(self, url: str, timeout=TIMEOUT) -> BeautifulSoup:
        r = self.sess.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code in RETRY_STATUS:
            for k in range(2):
                time.sleep(0.6 * (k+1))
                r = self.sess.get(url, headers=HEADERS, timeout=timeout)
                if r.status_code not in RETRY_STATUS:
                    break
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")

    def get_bytes(self, url: str, timeout=TIMEOUT) -> bytes:
        r = self.sess.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code in RETRY_STATUS:
            for k in range(2):
                time.sleep(0.6 * (k+1))
                r = self.sess.get(url, headers=HEADERS, timeout=timeout)
                if r.status_code not in RETRY_STATUS:
                    break
        r.raise_for_status()
        return r.content

HTTP = Http()

# Cache mật khẩu theo domain (WP đặt cookie wp-postpass_* theo site)
WP_PASS_CACHE: Dict[str, str] = {}

def maybe_unlock_protected(csoup: BeautifulSoup, page_url: str, pw_arg: Optional[str] = None) -> BeautifulSoup:
    # Nếu trang là bài viết WordPress đặt password (form.post-password-form),
    # sẽ hỏi mật khẩu (hoặc dùng pw_arg), submit tới action và re-fetch lại trang.
    # Thành công khi trang sau đó KHÔNG còn form password.
    try:
        form = csoup.select_one("form.post-password-form")
    except Exception:
        form = None
    if not form:
        return csoup

    from urllib.parse import urlparse, urljoin
    import getpass

    domain = urlparse(page_url).netloc
    import os
    password = (pw_arg or WP_PASS_CACHE.get(domain) or os.getenv('WP_PASS') or '').strip()

    # Lấy thông tin form
    action = form.get("action") or urljoin(page_url, "/wp-login.php?action=postpass")
    inp = form.find("input", {"name": "redirect_to"})
    redirect_to = inp.get("value") if inp and inp.get("value") else page_url

    tries = 3
    while tries > 0:
        if not password:
            try:
                password = getpass.getpass(f"🔒 Chương này có mật khẩu ({domain}). Nhập mật khẩu: ").strip()
            except Exception:
                password = input(f"🔒 Chương này có mật khẩu ({domain}). Nhập mật khẩu: ").strip()

        if not password:
            break

        data = {"post_password": password, "Submit": "Nhập", "redirect_to": redirect_to}
        try:
            # Gửi form để set cookie wp-postpass_*
            HTTP.sess.post(action, data=data, headers=HEADERS, timeout=TIMEOUT)
            # Re-fetch trang thực
            new_soup = HTTP.get_html(page_url)
            if not new_soup.select_one("form.post-password-form"):
                WP_PASS_CACHE[domain] = password
                return new_soup
        except Exception:
            pass

        print("❌ Mật khẩu sai hoặc không mở được. Thử lại...")
        password = ""
        tries -= 1

    print("⚠ Không mở được chương bị đặt mật khẩu — sẽ giữ nguyên nội dung thông báo.")
    return csoup


# ================== Parse Info + TOC =================
def get_info_from_index(soup: BeautifulSoup) -> Dict[str,str]:
    title_node = soup.find("h1", class_="entry-title")
    raw_title = _text(title_node)
    title, author = raw_title, "—"
    genre, status, cover_url = "", "", ""

    m = re.search(r"\[(.+?)\]\s*(.+?)\s*\|\s*(.+)", raw_title)
    if m:
        genre = m.group(1).strip()
        title = m.group(2).strip()
        author = m.group(3).strip()
    else:
        m2 = re.search(r"(.+?)\s*\|\s*(.+)", raw_title)
        if m2:
            title = m2.group(1).strip()
            author = m2.group(2).strip()

    # Cover: ảnh đầu trong bài
    img = soup.select_one("div.wp-block-image img[data-orig-file]")
    if img and img.get("data-orig-file"):
        cover_url = img["data-orig-file"]
    else:
        img2 = soup.select_one(".entry-content img")
        if img2 and img2.get("src"):
            cover_url = img2["src"]

    return {"title": title, "author": author, "genre": genre, "status": status, "cover": cover_url}

def get_chapter_list(soup: BeautifulSoup) -> List[Tuple[str,str]]:
    chapters: List[Tuple[str,str]] = []
    container = soup.find("div", class_="entry-content")
    if not container:
        return chapters

    prim = container.select("p.has-text-align-center[style*='font-size:24px'] a")
    if prim:
        for a in prim:
            t = _text(a); href = a.get("href")
            if t and href:
                chapters.append((t, href))

    if not chapters:
        for a in container.find_all("a"):
            t = _text(a); href = a.get("href")
            if not href: continue
            if ("chuong" in href.lower()) or t.lower().startswith("chương"):
                chapters.append((t or href, href))

    # unique theo href
    seen, uniq = set(), []
    for t,u in chapters:
        if u not in seen:
            uniq.append((t,u)); seen.add(u)
    return uniq

# ================== Làm sạch nội dung chương =================
def clean_chapter_html(soup: BeautifulSoup) -> str:
    def _txt(t: Tag | None) -> str:
        return t.get_text(" ", strip=True) if t else ""

    content = soup.find("div", class_="entry-content")
    if not content:
        return ""

    # GIỮ tiêu đề chương nếu đặt trong p.center (không xóa)
    title_p = content.find("p", class_="has-text-align-center")
    if title_p and title_p.find("strong"):
        if title_p.find("a"):
            for a in list(title_p.find_all("a")):
                a.unwrap()
        title_p["class"] = list(set((title_p.get("class") or []) + ["chapter-heading"]))

    # Xoá các khối rác rõ ràng
    for bad in content.select(
        '[id^="atatags-"], .sharedaddy, .sd-like-enabled, .sd-sharing-enabled, '
        '.sd-block, .sd-social, .robots-nocontent, .jetpack-likes-widget-wrapper, '
        '#jp-relatedposts, .jp-relatedposts, #wordads-inline-marker, '
        '.wp-block-social-links, script, style, iframe, hr.wp-block-separator'
    ):
        bad.decompose()

    # Điều hướng & disclaimer
    NAV_RE = re.compile(r"(giới thiệu|mục lục|chương\s*(sau|trước)|next|previous|prev|tiếp|trước)", re.I)
    ARROWS = ("→", "←", "&rarr;", "&larr;", "»", "«")
    DISMISS_RE = re.compile(r"(vui lòng chỉ đọc truyện|forum\.kites\.vn|shiningjustforme\.wordpress\.com)", re.I)

    def _is_pure_nav_or_disclaimer(ptag: Tag) -> bool:
        t = _txt(ptag)
        if not t:
            return True
        tl = t.lower()
        # Điều hướng rõ ràng
        if NAV_RE.search(tl) or any(sym in t for sym in ARROWS):
            if len(tl) <= 140 or len(ptag.find_all("a")) >= 1:
                return True
        # Disclaimer: chỉ xóa khi đoạn ngắn và hầu như chỉ có credit
        if DISMISS_RE.search(tl):
            tl_stripped = DISMISS_RE.sub("", tl)
            if len(tl_stripped.strip(" .…—-_*")) <= 8:
                return True
        return False

    for ptag in list(content.find_all("p")):
        if _is_pure_nav_or_disclaimer(ptag):
            ptag.decompose()

    # unwrap GIỮ <strong>
    for tag in content.select("em, span, font, b, i, u"):
        if tag.name != "strong":
            tag.unwrap()

    # Làm sạch <a> nhưng giữ href
    for a in content.find_all("a"):
        href = a.get("href")
        a.attrs = {}
        if href:
            a["href"] = href

    # Bỏ p rỗng
    for ptag in list(content.find_all("p")):
        if not ptag.get_text(strip=True) and not ptag.find("img"):
            ptag.decompose()

    # Trả về inner HTML
    html_str = "".join(str(c) for c in content.children).strip()

    # HẬU XỬ LÝ: gọt bỏ credit inline (không xóa cả đoạn)
    credit_pat = re.compile(
        r"(?is)(?:<[^>]+>)*[.…]*\s*Vui\s*lòng[^<]{0,200}?(?:forum\.?kites\.?vn|shiningjustforme\.wordpress\.com)[^<]{0,200}?[.…]*\s*(?:</[^>]+>)*"
    )
    html_str = credit_pat.sub("", html_str)

    # Dọn các <p> rỗng còn sót lại
    html_str = re.sub(r"(?is)<p>\s*(?:<br\s*/?>\s*)*</p>", "", html_str).strip()
    return html_str# ================== TXT extractor ====================
def html_to_plain_text(content_html: str) -> str:
    """Đổi phần inner HTML (đã sạch) -> plain text, mỗi <p> là 1 đoạn"""
    soup = BeautifulSoup(content_html, "html.parser")
    out_lines: List[str] = []
    for p in soup.find_all("p"):
        t = _text(p)
        if t:
            out_lines.append(t)
    # Nếu không có <p>, lấy text toàn node
    if not out_lines:
        t = soup.get_text("\n", strip=True)
        if t: out_lines.append(t)
    return "\n\n".join(out_lines).strip()

# ================== Cover helpers ====================
def _sniff_image_type(data: bytes):
    if not data or len(data) < 12: return (".bin", "application/octet-stream")
    if data[:3] == b"\xff\xd8\xff": return (".jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return (".png", "image/png")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return (".webp", "image/webp")
    if data[:6] in (b"GIF87a", b"GIF89a"): return (".gif", "image/gif")
    return (".bin", "application/octet-stream")

def HAS_PILOW():
    return HAS_PILLOW

def ensure_jpeg_cover(img_bytes: bytes):
    if not HAS_PILOW():
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)
    try:
        im = Image.open(_io.BytesIO(img_bytes))
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
            print(f"✔ Cover resized: {im.size[0]}x{im.size[1]}")
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
        print(f"⚠ Không convert cover sang JPEG: {e}")
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)

def load_cover(cover_in: Optional[str], story_url: Optional[str]) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    data = None
    if cover_in:
        try:
            if cover_in.lower().startswith(("http://","https://")):
                print(f"Tải cover: {cover_in}")
                data = HTTP.get_bytes(cover_in)
            else:
                print(f"Đọc cover: {cover_in}")
                with open(cover_in, "rb") as f:
                    data = f.read()
        except Exception as e:
            print(f"⚠ Cover lỗi '{cover_in}': {e}")

    if data is None and story_url:
        try:
            print("Tự lấy cover từ trang truyện…")
            soup = HTTP.get_html(story_url)
            img = soup.select_one("div.wp-block-image img[data-orig-file]") or soup.select_one(".entry-content img")
            if img:
                src = img.get("data-orig-file") or img.get("src")
                if src:
                    data = HTTP.get_bytes(src)
        except Exception as e:
            print(f"⚠ Không lấy được cover từ web: {e}")

    if data:
        return ensure_jpeg_cover(data)
    return (None, None, None)

# ================== EPUB builder =====================
def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)

def build_epub_from_htmls(epub_out_dir: str, title: str, author: str, html_paths: List[str],
                          cover_bytes=None, cover_ext=None, cover_mime=None, language="vi",
                          epub_target: str = "epub3", creator="Hishiro") -> str:
    if not html_paths:
        raise ValueError("Không có file HTML để đóng EPUB.")

    # sort theo index
    def _sort_key(p: str):
        base = os.path.splitext(os.path.basename(p))[0]
        m = re.match(r"^\s*(\d+)", base)
        idx = int(m.group(1)) if m else 10**9
        return (idx, base.casefold())
    html_paths = sorted(html_paths, key=_sort_key)

    out_is_dir = (os.path.isdir(epub_out_dir) or not epub_out_dir.lower().endswith(".epub"))
    if out_is_dir:
        os.makedirs(epub_out_dir, exist_ok=True)
        out_file = os.path.join(epub_out_dir, f"{_slugify_vi(title)}.epub")
    else:
        os.makedirs(os.path.dirname(epub_out_dir) or ".", exist_ok=True)
        out_file = epub_out_dir

    # chuẩn bị items
    items = []
    for path in html_paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = BeautifulSoup(f.read(), "html.parser")
            title_node = s.find("h1") or s.find("title")
            ctitle = title_node.get_text(strip=True) if title_node else os.path.basename(path)
            node = s.select_one("article") or s.body or s
            content_html = "".join(str(x) for x in node.children)
            items.append({"title": ctitle, "content_html": content_html})
        except Exception as e:
            print(f"WARN đọc HTML '{path}': {e}")

    if cover_bytes is not None and (not cover_ext or not cover_mime):
        cover_ext, cover_mime = _sniff_image_type(cover_bytes)

    cover_relpath = f"Images/cover{cover_ext}" if (cover_bytes and cover_ext) else None

    with zipfile.ZipFile(out_file, "w") as z:
        # mimetype
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        # container.xml
        container_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n'
            '</container>'
        ).encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)

        # cover.xhtml
        if cover_relpath:
            # tách href trước để tránh f-string có backslash replace
            cover_href = "../" + cover_relpath
            cover_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
                '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi">\n'
                '<head>\n'
                '  <title>Cover</title>\n'
                '  <meta charset="utf-8"/>\n'
                '  <style type="text/css">.cover{height:100vh;max-width:100%;object-fit:contain;margin:0 auto;display:block}</style>\n'
                '</head>\n'
                '<body>\n'
                '  <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                '       version="1.1" width="100%" height="100%" viewBox="0 0 1000 1500" '
                '       preserveAspectRatio="xMidYMid meet">\n'
                '    <image width="1000" height="1500" xlink:href="' + cover_href + '" class="cover"/>\n'
                '  </svg>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/Text/cover.xhtml", cover_xhtml)

        # CSS
        style_css = "body{font-family:serif;line-height:1.6}img{max-width:100%;height:auto}h1{font-size:1.4em;margin:0 0 .6em}"
        _epub_write(z, "OEBPS/Styles/style.css", style_css.encode("utf-8"))

        # manifest/spine/nav
        manifest_items, spine_items, navpoints = [], [], []
        chap_refs = []

        if cover_relpath:
            manifest_items.append('<item id="cover-html" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="cover-html" linear="no"/>')

        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            # tách content để tránh lỗi f-string backslash
            page_title = html.escape(c["title"])
            body_html  = c["content_html"]
            chapter_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi">\n'
                '<head>\n'
                '  <title>' + page_title + '</title>\n'
                '  <link href="../Styles/style.css" rel="stylesheet" type="text/css"/>\n'
                '  <meta charset="utf-8"/>\n'
                '</head>\n'
                '<body>\n'
                '  <h1>' + page_title + '</h1>\n'
                '  <div>' + body_html + '</div>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, f"OEBPS/{fn}", chapter_xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            navpoints.append(
                f'<navPoint id="navPoint-{i}" playOrder="{i}" class="chapter">'
                f'<navLabel><text>{html.escape(c["title"])}</text></navLabel>'
                f'<content src="{fn}"/></navPoint>'
            )
            chap_refs.append((fn, c["title"]))

        if cover_relpath and cover_bytes:
            _epub_write(z, f"OEBPS/{cover_relpath}", cover_bytes)

        manifest_items_str = "\n    ".join(manifest_items)
        spine_items_str    = "\n    ".join(spine_items)
        navpoints_str      = "\n    ".join(navpoints)
        dt_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cover_media_item = f'<item id="cover-image" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}"/>' if cover_relpath else ""

        if (epub_target or "epub3").lower().strip() == "epub3":
            # nav.xhtml
            nav_list_items = "\n".join(f'      <li><a href="{fn}">{html.escape(t)}</a></li>' for fn, t in chap_refs)
            nav_html = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi" xmlns:epub="http://www.idpf.org/2007/ops">\n'
                '<head>\n'
                '  <meta charset="utf-8"/>\n'
                '  <title>Table of Contents</title>\n'
                '  <link href="Styles/style.css" rel="stylesheet" type="text/css"/>\n'
                '</head>\n'
                '<body>\n'
                '  <nav epub:type="toc" id="toc">\n'
                '    <h1>' + html.escape(title) + '</h1>\n'
                '    <ol>\n' + nav_list_items + '\n'
                '    </ol>\n'
                '  </nav>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/nav.xhtml", nav_html)

            content_opf = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="3.0">\n'
                '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
                f'    <dc:identifier id="BookID">urn:uuid:{_slugify_vi(title)}-{int(time.time())}</dc:identifier>\n'
                f'    <dc:title>{html.escape(title)}</dc:title>\n'
                f'    <dc:creator>{html.escape(author)}</dc:creator>\n'
                '    <dc:language>vi</dc:language>\n'
                '    <meta name="creator" content="Hishiro"/>\n'
                f'    <dc:publisher>{html.escape(author or "—")}</dc:publisher>\n'
                f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
                '  </metadata>\n'
                '  <manifest>\n'
                f'    {cover_media_item}\n'
                '    <item id="css" href="Styles/style.css" media-type="text/css"/>\n'
                '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
                f'    {manifest_items_str}\n'
                '  </manifest>\n'
                '  <spine>\n'
                f'    {spine_items_str}\n'
                '  </spine>\n'
                '</package>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/content.opf", content_opf)

        else:
            # EPUB2
            manifest_cover = f'<item id="cover-image" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}"/>' if cover_relpath else ""
            guide_ref = '<reference type="cover" title="Cover" href="Text/cover.xhtml"/>' if cover_relpath else ""

            content_opf = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="2.0">\n'
                '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
                f'    <dc:identifier id="BookID">urn:uuid:{_slugify_vi(title)}-{int(time.time())}</dc:identifier>\n'
                f'    <dc:title>{html.escape(title)}</dc:title>\n'
                f'    <dc:creator>{html.escape(author)}</dc:creator>\n'
                f'    <dc:publisher>{html.escape(author or "—")}</dc:publisher>\n'
                '    <dc:language>vi</dc:language>\n'
                '    <meta name="cover" content="cover-image"/>\n'
                '    <meta name="creator" content="Hishiro"/>\n'
                f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
                '  </metadata>\n'
                '  <manifest>\n'
                f'    {manifest_cover}\n'
                '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
                '    <item id="css" href="Styles/style.css" media-type="text/css"/>\n'
                f'    {manifest_items_str}\n'
                '  </manifest>\n'
                '  <spine toc="ncx">\n'
                f'    {spine_items_str}\n'
                '  </spine>\n'
                '  <guide>\n'
                f'    {guide_ref}\n'
                '  </guide>\n'
                '</package>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/content.opf", content_opf)

            toc_ncx = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"\n'
                '  "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
                '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
                '  <head>\n'
                f'    <meta name="dtb:uid" content="urn:uuid:{_slugify_vi(title)}"/>\n'
                '    <meta name="dtb:depth" content="1"/>\n'
                '    <meta name="dtb:totalPageCount" content="0"/>\n'
                '    <meta name="dtb:maxPageNumber" content="0"/>\n'
                '  </head>\n'
                f'  <docTitle><text>{html.escape(title)}</text></docTitle>\n'
                '  <navMap>\n'
                f'    {navpoints_str}\n'
                '  </navMap>\n'
                '</ncx>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/toc.ncx", toc_ncx)

    return out_file

# ================== Save chương ======================
def save_chapter_html(out_dir: str, idx: int, chapter_title: str, content_html: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    base = f"{idx:04d} - {_slugify_vi(chapter_title) or 'chuong'}"
    path = os.path.join(out_dir, base + ".html")

    # Kiểm tra nội dung đã có tiêu đề "Chương ..." ở dòng đầu chưa
    soup_inner = BeautifulSoup(content_html, "html.parser")
    first_text = ""
    for node in soup_inner.find_all(["p","h1","h2","div","span"], recursive=True):
        t = _text(node)
        if t:
            first_text = t
            break

    add_h1 = True
    if first_text and re.match(r"^\s*chương\s*\d+", first_text, flags=re.I):
        add_h1 = False  # đã có tiêu đề chương trong nội dung

    body_parts = []
    if add_h1:
        body_parts.append('<h1>' + html.escape(chapter_title) + '</h1>')
    body_parts.append('<article>' + content_html + '</article>')

    doc = (
        '<!DOCTYPE html>\n'
        '<html lang="vi">\n'
        '<head>\n'
        '  <meta charset="utf-8"/>\n'
        '  <title>' + html.escape(chapter_title) + '</title>\n'
        '</head>\n'
        '<body>\n' +
        "\n".join(body_parts) + '\n'
        '</body>\n'
        '</html>\n'
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path

def save_chapter_txt(out_dir: str, idx: int, chapter_title: str, content_html: str) -> str:
    txt_dir = os.path.join(out_dir, "txt")
    os.makedirs(txt_dir, exist_ok=True)
    base = f"{idx:04d} - {_slugify_vi(chapter_title) or 'chuong'}"
    path = os.path.join(txt_dir, base + ".txt")
    plain = html_to_plain_text(content_html)
    with open(path, "w", encoding="utf-8") as f:
        f.write(chapter_title.strip() + "\n\n" + plain + "\n")
    return path

# ================== Pipeline tác vụ ==================
def download_htmls(index_url: str, out_base=DEFAULT_OUT, start=1, end=None, resume=False) -> Tuple[str, Dict[str,str], List[str]]:
    soup = HTTP.get_html(index_url)
    info = get_info_from_index(soup)
    title = info.get("title") or "Truyện"
    story_slug = _slugify_vi(title)
    html_dir = os.path.join(out_base, story_slug)

    chapters = get_chapter_list(soup)
    if not chapters:
        raise RuntimeError("Không tìm thấy danh sách chương.")
    if end is None:
        end = len(chapters)
    start = max(1, int(start)); end = max(start, min(end, len(chapters)))

    html_paths: List[str] = []
    print(f"⬇️  Tải {end-start+1} chương…")
    for i, (ctitle, curl) in enumerate(chapters[start-1:end], start=start):
        save_path = os.path.join(html_dir, f"{i:04d} - {_slugify_vi(ctitle or f'chuong-{i}')}.html")
        if resume and os.path.isfile(save_path):
            print(f"[{i:04d}] SKIP — {ctitle}")
            html_paths.append(save_path)
            continue
        try:
            
            csoup = HTTP.get_html(curl)
            csoup = maybe_unlock_protected(csoup, curl)
            # Nếu vẫn còn form password sau khi thử, bỏ qua chương
            try:
                still_locked = csoup.select_one("form.post-password-form") is not None
            except Exception:
                still_locked = False
            if still_locked:
                print(f"[{i:04d}] LOCKED — bỏ qua: {ctitle}")
                continue
            content_html = clean_chapter_html(csoup)

            if not content_html:
                node = csoup.find("div", class_="entry-content")
                content_html = node.decode() if node else ""
            saved = save_chapter_html(html_dir, i, ctitle or f"Chương {i}", content_html)
            html_paths.append(saved)
            print(f"[{i:04d}] Saved — {ctitle}")
        except Exception as e:
            print(f"[{i:04d}] ERROR: {e}")
        time.sleep(SLEEP_BETWEEN_CHAPS)

    return html_dir, info, html_paths

def ensure_txts_from_htmls(html_dir: str) -> List[str]:
    txt_files: List[str] = []
    names = sorted([n for n in os.listdir(html_dir) if n.lower().endswith(".html")])
    for name in names:
        path = os.path.join(html_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = BeautifulSoup(f.read(), "html.parser")
            title = s.find("h1")
            ctitle = title.get_text(strip=True) if title else os.path.splitext(name)[0]
            node = s.select_one("article") or s.body or s
            content_html = "".join(str(x) for x in node.children)
            txt_path = save_chapter_txt(html_dir, int(name[:4]), ctitle, content_html)
            txt_files.append(txt_path)
        except Exception as e:
            print(f"WARN TXT '{name}': {e}")
    return txt_files

def build_epub(index_url: str, out_base=DEFAULT_OUT, cover_in: Optional[str]=None, epub3=True) -> str:
    soup = HTTP.get_html(index_url)
    info = get_info_from_index(soup)
    title = info.get("title") or "Truyện"
    story_slug = _slugify_vi(title)
    html_dir = os.path.join(out_base, story_slug)

    if not os.path.isdir(html_dir):
        raise RuntimeError("Chưa có HTML để đóng EPUB. Hãy chọn tuỳ chọn có tải HTML trước.")

    html_paths = [os.path.join(html_dir, n) for n in os.listdir(html_dir) if n.lower().endswith(".html")]
    if not html_paths:
        raise RuntimeError("Không tìm thấy file HTML trong thư mục truyện.")

    cover_bytes, cover_ext, cover_mime = load_cover(cover_in, index_url)

    print("📦 Đóng EPUB…")
    out_file = build_epub_from_htmls(
        epub_out_dir=out_base,
        title=title,
        author=info.get("author") or "—",
        html_paths=html_paths,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime,
        epub_target="epub3" if epub3 else "epub2"
    )
    return out_file

# ================== MENU (layout quen thuộc) ================
def main():
    print("→ Ánh xạ domain thành module: shiningjustforme_wp")
    print("Đã import module: shiningjustforme_wp")
    url = input("Nhập URL (trang Giới thiệu/Mục lục): ").strip()
    if not url:
        print("URL trống."); return

    cover_in = input("Nhập đường dẫn Cover (bỏ trống để tự lấy): ").strip() or None
    try:
        start = int((input("Chương bắt đầu (mặc định 1): ").strip() or "1"))
    except:
        start = 1
    end_in = input("Chương kết thúc (Enter = chương cuối): ").strip()
    end = int(end_in) if end_in.isdigit() else None
    resume = (input("Resume (bỏ qua chương đã có)? [y/N]: ").strip().lower() == "y")

    print("\nChọn chế độ:")
    print("[1]: Tải & lưu HTML")
    print("[2]: Tải & lưu TXT (trích từ HTML; nếu chưa có HTML sẽ tải trước)")
    print("[3]: Tải & lưu HTML + TXT")
    print("[4]: Tải & lưu HTML + Build EPUB")
    print("[5]: Tải & lưu TXT + Build EPUB (sẽ tải mới nếu cần)")
    print("[6]: Tải & lưu HTML + TXT + Build EPUB")
    choice = (input("→ Lựa chọn: ").strip() or "3")

    # EPUB target
    epub_target = input("Định dạng EPUB [3/2] (Enter=3): ").strip()
    epub3 = (epub_target != "2")

    try:
        if choice == "1":
            html_dir, info, html_paths = download_htmls(url, DEFAULT_OUT, start, end, resume)
            print(f"✓ Đã lưu HTML tại: {html_dir}")

        elif choice == "2":
            # cần HTML, nếu chưa có thì tải
            soup = HTTP.get_html(url)
            info = get_info_from_index(soup)
            story_slug = _slugify_vi(info.get("title") or "truyen")
            html_dir = os.path.join(DEFAULT_OUT, story_slug)
            if not os.path.isdir(html_dir) or not any(n.lower().endswith(".html") for n in os.listdir(html_dir)):
                print("Chưa có HTML → Tải trước…")
                download_htmls(url, DEFAULT_OUT, start, end, resume)
            txt_files = ensure_txts_from_htmls(html_dir)
            print(f"✓ TXT đã tạo: {len(txt_files)} file → {os.path.join(html_dir,'txt')}")

        elif choice == "3":
            html_dir, info, html_paths = download_htmls(url, DEFAULT_OUT, start, end, resume)
            txt_files = ensure_txts_from_htmls(html_dir)
            print(f"✓ HTML + TXT đã lưu. TXT: {len(txt_files)} file")

        elif choice == "4":
            html_dir, info, html_paths = download_htmls(url, DEFAULT_OUT, start, end, resume)
            out_file = build_epub(url, DEFAULT_OUT, cover_in, epub3)
            print(f"✔ EPUB: {out_file}")

        elif choice == "5":
            # TXT + EPUB (tải mới nếu cần)
            soup = HTTP.get_html(url)
            info = get_info_from_index(soup)
            story_slug = _slugify_vi(info.get("title") or "truyen")
            html_dir = os.path.join(DEFAULT_OUT, story_slug)
            if not os.path.isdir(html_dir) or not any(n.lower().endswith(".html") for n in os.listdir(html_dir)):
                print("Chưa có HTML → Tải trước…")
                download_htmls(url, DEFAULT_OUT, start, end, resume)
            txt_files = ensure_txts_from_htmls(html_dir)
            out_file = build_epub(url, DEFAULT_OUT, cover_in, epub3)
            print(f"✓ TXT: {len(txt_files)} file\n✔ EPUB: {out_file}")

        elif choice == "6":
            html_dir, info, html_paths = download_htmls(url, DEFAULT_OUT, start, end, resume)
            txt_files = ensure_txts_from_htmls(html_dir)
            out_file = build_epub(url, DEFAULT_OUT, cover_in, epub3)
            print(f"✓ TXT: {len(txt_files)} file\n✔ EPUB: {out_file}")

        else:
            print("Lựa chọn không hợp lệ.")
    except KeyboardInterrupt:
        print("\n⛔ Đã hủy.")
    except Exception as e:
        print(f"❌ Lỗi: {e}")
        sys.exit(2)

if __name__ == "__main__":
    main()
