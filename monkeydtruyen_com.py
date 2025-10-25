# -*- coding: utf-8 -*-
"""
monkeydtruyen_com.py — Downloader + EPUB builder cho monkeydtruyen.com

Các chế độ:
  [1] HTML
  [2] TXT (lấy web → TXT trực tiếp)
  [3] HTML + TXT (TXT trích từ HTML đã tải)
  [4] HTML + Build EPUB
  [5] TXT  + Build EPUB
  [6] HTML + TXT + Build EPUB

- Khôi phục chữ bị “giấu” bằng CSS ::before/::after (content: "...").
- Cover: ưu tiên input (URL/file), nếu trống → tự lấy từ trang truyện, chuyển JPEG & resize nếu có Pillow.
- EPUB2/EPUB3 có cover.xhtml (thân thiện Kobo).
"""

from bs4 import BeautifulSoup
from typing import Optional, List, Dict, Tuple
from urllib.parse import urljoin
import requests, re, html, os, unicodedata, zipfile, glob, time, datetime as dt

# =================== CẤU HÌNH ===================
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}
EPUB_TARGET = "epub3"   # mặc định; khi build sẽ hỏi lại

MAX_COVER_SIZE = (1600, 2400)  # Resize tối đa (w,h) cho cover

# Thử import Pillow cho xử lý ảnh bìa
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("⚠ Cảnh báo: Không tìm thấy thư viện Pillow. Không thể tối ưu/chuyển đổi cover sang JPEG.")

# =================== TIỆN ÍCH CHUNG ===================
def _text(el) -> str:
    """Lấy text an toàn từ thẻ BeautifulSoup."""
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""

def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    """Tải HTML với retry hợp lý."""
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
        soup.base_url = url  # lưu base cho urljoin
        return soup
    # không tới đây
    return BeautifulSoup("", "html.parser")

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

# Bỏ đoạn rác / quảng cáo / mồi Shopee
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
    # dòng chỉ có dấu hoặc quá ngắn (kiểu "." hay "—")
    if len(t) <= 2 and all(ch in ".•*·-—–" or ch.isspace() for ch in t):
        return True
    return False



# =================== LẤY INFO TRUYỆN ===================
def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    """Đọc title/author/genre/status theo layout monkeydtruyen."""
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
        # Thể loại có thể trong <dd> chứa nhiều <a>
        genres = "N/A"
        idx = [i for i, dt_node in enumerate(dts) if _text(dt_node) == "Thể loại"]
        if idx:
            gdd = dds[idx[0]]
            tags = [a.get_text(strip=True) for a in gdd.select("a")]
            genres = " - ".join(tags) if tags else _text(gdd)

        status = label_to_val.get("Trạng thái", "N/A")

        info["title"]  = title
        info["author"] = author
        info["genre"]  = genres
        info["status"] = status
    except Exception:
        pass
    return info

def _get_cover_link(soup: BeautifulSoup) -> str:
    """Ảnh bìa trong trang truyện."""
    try:
        n = soup.select_one("img.img-fluid")
        return n.get("src") if n and n.get("src") else ""
    except Exception:
        return ""

# =================== DANH SÁCH CHƯƠNG ===================
def _get_list_chapters(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """
    Lấy danh sách chương theo layout:
    <div class="list-chapters">
      <div class="episode-title"><a href="/.../chuong-1.html">Chương 1 …</a></div>
    """
    chapters: List[Dict[str, str]] = []
    try:
        for a in soup.select("div.list-chapters div.episode-title a[href]"):
            title = _text(a)
            href  = a.get("href")
            if not title or not href:
                continue
            if not href.startswith("http"):
                href = urljoin(soup.base_url or "", href)
            chapters.append({"title": title, "url": href})
    except Exception:
        pass
    # thường trang liệt kê theo mới → cũ, ta đảo lại để từ 1 → N
    return list(reversed(chapters))

# =================== GIẢI MÃ CSS ::before/::after ===================
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

    # inline <style>
    for st in soup.find_all("style"):
        if st.string:
            css_texts.append(st.string)

    # link stylesheet / preload as=style / *.css
    for link in soup.find_all("link"):
        rel = [x.lower() for x in (link.get("rel") or [])]
        as_attr = (link.get("as") or "").lower()
        href = link.get("href")
        if not href:
            continue
        take = False
        if any("stylesheet" in x for x in rel): take = True
        if ("preload" in rel and as_attr == "style"): take = True
        if href.endswith(".css"): take = True
        if not take: continue

        css_url = urljoin(base_url, href)
        try:
            r = requests.get(css_url, headers=HEADERS, timeout=30)
            if r.ok:
                text = r.text
                css_texts.append(text)
                # theo @import (1 tầng)
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
    """
    Lôi content text từ CSS cho các class có :before/:after
    Hỗ trợ:
      .abc::before { content: "x" "y" "\006B\0068\00F4\006E\0067" }
      .abc:after  { content: '...' }
      .a:before,.b:before{content:'...'}
    """
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
            # ghép tất cả chuỗi trong content:
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

# =================== LẤY NỘI DUNG CHƯƠNG ===================
def _pick_chapter_title(soup: BeautifulSoup) -> Optional[str]:
    for sel in ["h1.card-title", "h1.title", "h1", "h2.title", "h2"]:
        n = soup.select_one(sel)
        if n and _text(n):
            return _text(n)
    h = soup.find(["h1", "h2", "h3"])
    return _text(h) if h else None

def _container_to_clean_html(container: BeautifulSoup) -> str:
    # bỏ script/style/iframe, các p rỗng, normalize <br>
    for sel in ["script", "style", "noscript", "iframe", "form"]:
        for t in container.select(sel):
            t.decompose()
    html_str = str(container)
    html_str = re.sub(r"<br\s*>", "<br/>", html_str, flags=re.I)
    html_str = re.sub(r"<p>\s*(?:&nbsp;|\u00A0|\s)*</p>", "", html_str, flags=re.I)
    return html_str

def fetch_chapter_content(url: str) -> Dict[str, str]:
    """Lấy nội dung chương + khôi phục chữ span; KHÔNG chèn \n giữa mỗi span."""
    soup = _fetch_html(url)

    # Map CSS → text rồi thay vào DOM
    css_texts = _collect_css_texts(soup, url)
    cls_map   = _build_span_map_from_css(css_texts)
    _replace_spans_with_text(soup, cls_map)

    title = _pick_chapter_title(soup) or "Chương"
    container = soup.select_one("div#chapter-content-render")
    if not container:
        divs = sorted(soup.find_all("div"), key=lambda d: len(d.get_text(" ", strip=True)), reverse=True)
        container = divs[0] if divs else soup

    # Sau khi thay span→text, duyệt từng <p>; 
    # - đổi <br> thành '\n'
    # - LẤY TEXT KHÔNG DÙNG sep="\n" (tránh mỗi span thành 1 dòng)
    # - lọc rác Shopee
    parts: List[str] = []
    ps = container.find_all("p")
    if ps:
        for p in ps:
            for br in p.find_all("br"):
                br.replace_with("\n")
            t = p.get_text()                   # KHÔNG truyền sep => không thêm \n giữa span
            t = t.replace("\xa0", " ")
            # dọn khoảng trắng thừa quanh newline
            t = re.sub(r"[ \t]+\n", "\n", t)
            t = re.sub(r"\n[ \t]+", "\n", t)
            t = re.sub(r" {2,}", " ", t)
            if _is_noise_paragraph(t):
                continue
            safe = html.escape(t.strip()).replace("\n", "<br/>")
            parts.append(f"<p>{safe}</p>")
        content_html = "\n".join(parts) if parts else "<p>(Trống)</p>"
    else:
        # fallback: giữ nguyên container đã làm sạch cơ bản
        content_html = _container_to_clean_html(container) or "<p>(Trống)</p>"

    return {"title": title, "content_html": content_html, "url": url}

# =================== LƯU HTML/TXT ===================
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
    path  = os.path.join(out_dir, fname)
    html_out = HTML_TEMPLATE.format(
        doc_title     = f"{book_title} - {chap.get('title') or f'Chương {chapter_idx}'}",
        chapter_title = html.escape(chap.get('title') or f"Chương {chapter_idx}"),
        book_title    = html.escape(book_title or "Truyện"),
        src           = chap.get("url") or "",
        content       = chap.get("content_html") or "<p>(Không có nội dung)</p>",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_out)
    return path

def save_all_chapters_to_html(book_title: str, chapters: list, out_dir: str,
                              start: int = 1, end: Optional[int] = None) -> List[str]:
    n = len(chapters)
    if end is None or end > n: end = n
    saved: List[str] = []
    for i in range(start, end + 1):
        info = chapters[i-1]
        try:
            chap = fetch_chapter_content(info["url"])
            if not chap.get("title"): chap["title"] = info.get("title")
            p = save_chapter_html(book_title, i, chap, out_dir)
            print(f"[{i:04d}/{n}] Saved HTML: {p}", flush=True)
            saved.append(p)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"[{i:04d}/{n}] ERROR {info.get('url')}: {e}", flush=True)
    return saved

def _html_article_text(soup: BeautifulSoup) -> str:
    for br in soup.find_all("br"):
        br.replace_with("\n")
    node = soup.select_one("article") or soup.body or soup
    txt = node.get_text("\n", strip=True)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt

def convert_htmls_to_txts(out_dir: str, only_paths: Optional[List[str]] = None) -> List[str]:
    html_files = sorted(only_paths) if only_paths else sorted(glob.glob(os.path.join(out_dir, "*.html")))
    saved_txts: List[str] = []
    for path in html_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), "html.parser")
            txt = _html_article_text(soup)
            base = os.path.splitext(os.path.basename(path))[0]
            out_txt = os.path.join(out_dir, f"{base}.txt")
            with open(out_txt, "w", encoding="utf-8") as wf:
                wf.write(txt)
            print(f"→ TXT: {out_txt}")
            saved_txts.append(out_txt)
        except Exception as e:
            print(f"ERR convert_htmls_to_txts({path}): {e}")
    return saved_txts

def save_all_chapters_to_txt(book_title: str, chapters: list, out_dir: str,
                             start: int = 1, end: Optional[int] = None) -> List[str]:
    n = len(chapters)
    if end is None or end > n: end = n
    os.makedirs(out_dir, exist_ok=True)
    saved: List[str] = []

    for i in range(start, end + 1):
        info = chapters[i - 1]
        try:
            c = fetch_chapter_content(info["url"])
            if not c.get("title"):
                c["title"] = info.get("title") or f"Chương {i}"
            soup = BeautifulSoup(c.get("content_html") or "", "html.parser")
            txt = _html_article_text(soup)
            fname = f"{i:04d} - {_safe_filename(c['title'])}.txt"
            path = os.path.join(out_dir, fname)
            with open(path, "w", encoding="utf-8") as f:
                f.write(txt)
            print(f"[{i:04d}/{n}] Saved TXT: {path}", flush=True)
            saved.append(path)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"[{i:04d}/{n}] ERROR {info.get('url')}: {e}", flush=True)
    return saved

def _txt_to_html_content(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    html_blocks = []
    for b in blocks:
        safe = html.escape(b).replace("\n", "<br/>")
        html_blocks.append(f"<p>{safe}</p>")
    return "\n".join(html_blocks) if html_blocks else "<p>(Trống)</p>"

def convert_txts_to_htmls(out_dir: str, book_title: Optional[str] = None) -> List[str]:
    saved: List[str] = []
    if not book_title:
        book_title = os.path.basename(os.path.normpath(out_dir)) or "Truyện"
    txt_files = sorted(glob.glob(os.path.join(out_dir, "*.txt")))
    for idx, path in enumerate(txt_files, 1):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            base = os.path.splitext(os.path.basename(path))[0]
            m = re.match(r"^\s*(\d+)\s*-\s*(.+)$", base)
            if m:
                chapter_idx = int(m.group(1))
                title_guess = m.group(2).strip()
            else:
                chapter_idx = idx
                title_guess = next((ln.strip() for ln in raw.splitlines() if ln.strip()), f"Chương {chapter_idx}")
            content_html = _txt_to_html_content(raw)
            chap = {"title": title_guess, "content_html": content_html, "url": ""}
            out_html = save_chapter_html(book_title, chapter_idx, chap, out_dir)
            saved.append(out_html)
            print(f"→ HTML: {out_html}")
        except Exception as e:
            print(f"ERR convert_txts_to_htmls({path}): {e}")
    return sorted(saved)

# =================== COVER (LOAD & CONVERT) ===================
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
        import io as _io
        im = Image.open(_io.BytesIO(img_bytes))
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
            print(f"✔ Cover resized to: {im.size[0]}x{im.size[1]}")
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
        print(f"⚠ Không convert cover sang JPEG: {e}. Dùng ảnh gốc {ext}.")
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

def _load_cover_from_input(cover_in: str, story_url: Optional[str]):
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
            print(f"⚠ Không tải/đọc cover '{cover_in}': {e}")
    if data is None and story_url:
        try:
            print("Tự lấy cover từ trang truyện…")
            cb = _fetch_cover_from_book_page(story_url)
            if cb:
                data = cb
        except Exception as e:
            print(f"⚠ Không lấy được cover từ web: {e}")
    if data:
        return _ensure_jpeg_cover(data)
    return (None, None, None)

# =================== EPUB BUILDER (EPUB2/EPUB3) ===================
def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)

def create_epub(book_url: Optional[str],
                book_title: str,
                author: str,
                chapters,                  # list[str path_html] hoặc list[dict{url,title?}]
                out_epub_path: str,        # file .epub hoặc thư mục
                creator: str = "Hishiro",
                language: str = "vi",
                epub_target: Optional[str] = None,
                cover_bytes: Optional[bytes] = None,
                cover_ext: Optional[str] = None,
                cover_mime: Optional[str] = None) -> str:

    book_title = book_title or "Truyện"
    author     = author or "—"
    target     = (epub_target or EPUB_TARGET).lower().strip()

    out_is_dir = (os.path.isdir(out_epub_path) or not out_epub_path.lower().endswith(".epub"))
    if out_is_dir:
        os.makedirs(out_epub_path, exist_ok=True)
        out_file = os.path.join(out_epub_path, f"{_slugify_vi(book_title)}.epub")
    else:
        os.makedirs(os.path.dirname(out_epub_path) or ".", exist_ok=True)
        out_file = out_epub_path

    # Thu thập nội dung
    items = []
    if chapters and isinstance(chapters[0], str):
        # Đọc HTML cục bộ
        for i, html_path in enumerate(chapters, 1):
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    soup = BeautifulSoup(f.read(), "html.parser")
                title_node = soup.find("h1") or soup.find("title")
                if title_node:
                    title = title_node.get_text(strip=True)
                else:
                    base = os.path.splitext(os.path.basename(html_path))[0]
                    m = re.match(r"^\s*\d+\s*-\s*(.+)$", base)
                    title = m.group(1).strip() if m else base
                node = soup.select_one("article") or soup.body or soup
                content_html = "".join(str(x) for x in node.children)
                items.append({"title": title, "content_html": content_html})
            except Exception as e:
                print(f"WARN đọc HTML '{html_path}': {e}")
    else:
        # Fetch từ web
        for idx, info in enumerate(chapters or [], 1):
            c = fetch_chapter_content(info["url"])
            if not c.get("title"):
                c["title"] = info.get("title") or f"Chương {idx}"
            items.append(c)
            time.sleep(SLEEP_BETWEEN_CHAPS)

    # Cover fallback nếu thiếu
    if cover_bytes is None and book_url:
        try:
            print("Fallback cover (trong create_epub)…")
            cb = _fetch_cover_from_book_page(book_url)
            if cb:
                cover_bytes, cover_ext, cover_mime = _ensure_jpeg_cover(cb)
        except Exception:
            cover_bytes, cover_ext, cover_mime = None, None, None

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

        # cover.xhtml (cho cả epub2/3)
        if cover_relpath:
            cover_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi">\n'
                '<head>\n'
                '  <title>Cover</title>\n'
                '  <meta charset="utf-8"/>\n'
                '  <style type="text/css">.cover{height:100vh;max-width:100%;object-fit:contain;margin:0 auto;display:block}</style>\n'
                '</head>\n'
                '<body>\n'
                f'  <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="100%" height="100%" viewBox="0 0 1000 1500" preserveAspectRatio="xMidYMid meet">\n'
                f'    <image width="1000" height="1500" xlink:href="../{cover_relpath}" class="cover"/>\n'
                f'  </svg>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/Text/cover.xhtml", cover_xhtml)

        # CSS
        style_css = (
            "body{font-family:serif;line-height:1.6}"
            "img{max-width:100%;height:auto}"
            "h1{font-size:1.4em;margin:0 0 .6em}"
        )
        _epub_write(z, "OEBPS/Styles/style.css", style_css.encode("utf-8"))

        # Chương
        manifest_items, spine_items, navpoints = [], [], []
        chap_refs = []

        if cover_relpath:
            manifest_items.append('<item id="cover-html" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="cover-html" linear="no"/>')

        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            chapter_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">\n'
                '<head>\n'
                f'  <title>{html.escape(c["title"])}</title>\n'
                '  <link href="../Styles/style.css" rel="stylesheet" type="text/css"/>\n'
                '  <meta charset="utf-8"/>\n'
                '</head>\n'
                '<body>\n'
                f'  <h1>{html.escape(c["title"])}</h1>\n'
                f'  <div>{c["content_html"]}</div>\n'
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

        # Cover image
        if cover_relpath and cover_bytes:
            _epub_write(z, f"OEBPS/{cover_relpath}", cover_bytes)

        manifest_items_str = "\n    ".join(manifest_items)
        spine_items_str    = "\n    ".join(spine_items)
        navpoints_str      = "\n    ".join(navpoints)

        dt_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cover_media_item = f'<item id="cover-image" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}"/>' if cover_relpath else ""

        if target == "epub3":
            # nav.xhtml
            nav_list_items = "\n".join(f'      <li><a href="{fn}">{html.escape(t)}</a></li>' for fn, t in chap_refs)
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
                '    <ol>\n'
                f'{nav_list_items}\n'
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
                f'    <dc:identifier id="BookID">urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}</dc:identifier>\n'
                f'    <dc:title>{html.escape(book_title)}</dc:title>\n'
                f'    <dc:creator>{html.escape(author)}</dc:creator>\n'
                f'    <dc:language>{language}</dc:language>\n'
                f'    <meta name="creator" content="{html.escape(creator)}"/>\n'
                f'    <dc:publisher>Hishiro</dc:publisher>\n'
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
            # EPUB2: toc.ncx + opf 2.0
            if cover_relpath:
                manifest_cover = f'<item id="cover-image" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}"/>'
                guide_ref = f'<reference type="cover" title="Cover" href="Text/cover.xhtml"/>'
            else:
                manifest_cover = ""
                guide_ref = ""

            content_opf = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="2.0">\n'
                '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
                f'    <dc:identifier id="BookID">urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}</dc:identifier>\n'
                f'    <dc:title>{html.escape(book_title)}</dc:title>\n'
                f'    <dc:creator>{html.escape(author)}</dc:creator>\n'
                f'    <dc:publisher>{html.escape(author)}</dc:publisher>\n'
                f'    <dc:language>{language}</dc:language>\n'
                '    <meta name="cover" content="cover-image"/>\n'
                f'    <meta name="creator" content="{html.escape(creator)}"/>\n'
                f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
                '  </metadata>\n'
                '  <manifest>\n'
                f'    {cover_media_item}\n'
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
                f'    <meta name="dtb:uid" content="urn:uuid:{_slugify_vi(book_title)}"/>\n'
                '    <meta name="dtb:depth" content="1"/>\n'
                '    <meta name="dtb:totalPageCount" content="0"/>\n'
                '    <meta name="dtb:maxPageNumber" content="0"/>\n'
                '  </head>\n'
                f'  <docTitle><text>{html.escape(book_title)}</text></docTitle>\n'
                '  <navMap>\n'
                f'    {navpoints_str}\n'
                '  </navMap>\n'
                '</ncx>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/toc.ncx", toc_ncx)

    return out_file

def build_epub(epub_out_dir: str, title: str, author: str, html_paths: List[str],
               cover_bytes=None, cover_ext=None, cover_mime=None, epub_target: Optional[str] = None) -> str:
    paths = [p for p in (html_paths or []) if isinstance(p, str) and p.lower().endswith(".html") and os.path.isfile(p)]
    if not paths:
        raise ValueError("build_epub: Không có file HTML hợp lệ để đóng EPUB.")

    def _sort_key(p: str):
        base = os.path.splitext(os.path.basename(p))[0]
        m = re.match(r"^\s*(\d+)", base)
        idx = int(m.group(1)) if m else 10**9
        return (idx, base.casefold())
    paths.sort(key=_sort_key)

    return create_epub(
        book_url=None,
        book_title=title,
        author=author,
        chapters=paths,
        out_epub_path=epub_out_dir,
        creator="Hishiro",
        language="vi",
        epub_target=epub_target or EPUB_TARGET,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime
    )

def _delete_files(paths: List[str]):
    ok = 0
    for p in paths or []:
        try:
            if os.path.isfile(p):
                os.remove(p)
                ok += 1
        except Exception as e:
            print(f"⚠ Không xóa được {p}: {e}")
    print(f"🧹 Đã xóa {ok}/{len(paths or [])} file")

# =================== MAIN ===================
def main():
    urlStory = input("Nhập URL: ").strip()
    cover_in = input("Nhập đường dẫn Cover (bỏ trống để tự lấy từ web): ").strip()

    soup = _fetch_html(urlStory)
    info = _get_book_info(soup)

    print('-----------------THÔNG TIN TRUYỆN:-------------------')
    print(f"Title : {info.get('title','')}")
    print(f"Author: {info.get('author','')}")
    print(f"Genre : {info.get('genre','')}")
    print(f"Status: {info.get('status','')}")
    print('-----------------------------------------------------')

    chapters = _get_list_chapters(soup)
    print(f"Total chapters: {len(chapters)}")
    if chapters:
        print('chapters[1]: ', chapters[0])
        print('chapters[last]: ', chapters[-1])

    # Chuẩn bị cover (local/URL/auto)
    cover_bytes, cover_ext, cover_mime = _load_cover_from_input(cover_in, urlStory)
    print(f"Cover status: {'OK' if cover_bytes else 'MISSING'} ({cover_mime})")

    print("\nChọn chức năng:")
    print("[1]: Tải và lưu dạng HTML")
    print("[2]: Tải và lưu dạng TXT (Chuyển đổi từ HTML, sau đó xóa HTML)")
    print("[3]: Tải và lưu dạng HTML + TXT")
    print("[4]: Tải và lưu dạng HTML + Build Epub")
    print("[5]: Tải và lưu dạng TXT + Build Epub")
    print("[6]: Tải và lưu dạng HTML + TXT + Build Epub")
    choice = input("Nhập lựa chọn (1-6): ").strip()

    # Hỏi target EPUB nếu có build (4/5/6)
    do_epub = choice in {"4", "5", "6"}
    if do_epub:
        global EPUB_TARGET
        target_in = input(f"Chọn EPUB target (2=EPUB 2, 3=EPUB 3 compat) [{EPUB_TARGET[4]}]: ").strip()
        EPUB_TARGET = "epub3" if target_in == "3" else "epub2"
        print(f"EPUB target: {EPUB_TARGET}")
        if not HAS_PILLOW and EPUB_TARGET in ["epub2", "epub3"]:
            print("⚠️ EPUB cover có thể không hiển thị tối ưu trên Kobo nếu thiếu Pillow (JPEG/Resize).")

    # Thư mục đầu ra
    out_dir = os.path.join("output", _slugify_vi(info.get("title") or "truyen"))
    epub_out_dir = "output"
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(epub_out_dir, exist_ok=True)
    print("Output dir:", out_dir)

    saved_htmls: List[str] = []
    saved_txts:  List[str] = []
    epub_path:   Optional[str] = None

    if choice in {"1", "3", "4", "5", "6"}:
        print(f"\n[Mode {choice}] Tải HTML…")
        saved_htmls = save_all_chapters_to_html(info.get("title") or "Truyện", chapters, out_dir, start=1, end=None)
        print(f"✔ Đã lưu HTML: {len(saved_htmls)} files.")

    if choice in {"2", "3", "5", "6"}:
        if choice == "2":
            print(f"\n[Mode 2] Tải TXT trực tiếp…")
            saved_txts = save_all_chapters_to_txt(info.get("title") or "Truyện", chapters, out_dir, start=1, end=None)
            print(f"✔ Đã lưu TXT: {len(saved_txts)} files.")
        else:
            print(f"\n[Mode {choice}] Trích TXT từ HTML đã tải…")
            saved_txts = convert_htmls_to_txts(out_dir, only_paths=saved_htmls)
            print(f"✔ Đã lưu TXT: {len(saved_txts)} files.")

    if choice in {"4", "5", "6"}:
        print(f"\n[Mode {choice}] Build EPUB…")
        if not saved_htmls:
            print("⚠️ Cần HTML để build EPUB. Đang tải HTML…")
            saved_htmls = save_all_chapters_to_html(info.get("title") or "Truyện", chapters, out_dir, start=1, end=None)
        if saved_htmls:
            epub_path = build_epub(epub_out_dir, info.get("title") or "Truyện", info.get("author") or "—",
                                   saved_htmls, cover_bytes=cover_bytes, cover_ext=cover_ext, cover_mime=cover_mime,
                                   epub_target=EPUB_TARGET)
            print(f"✔ EPUB: {epub_path}")

    # Xóa HTML nếu chọn TXT-only (2) hoặc TXT+EPUB (5)
    if choice in {"2", "5"}:
        print("\n🧹 Xóa file HTML trung gian…")
        _delete_files(saved_htmls)
        saved_htmls = []

    print("\n------------------- DONE -------------------")
    if epub_path:
        print(f"✅ Đã tạo EPUB: {epub_path}")
    if saved_htmls:
        print(f"✅ HTML lưu tại: {out_dir}")
    if saved_txts:
        print(f"✅ TXT  lưu tại: {out_dir}")

if __name__ == "__main__":
    main()
