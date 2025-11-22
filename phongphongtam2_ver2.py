# -*- coding: utf-8 -*-
'''
    @Author: Hishio - Cá mụp cắn cáp
    @github: https://github.com/hishirooo
    @date: 20-11-2025
    @version: 1.5 - Sử dụng logic đóng gói EPUB thủ công từ bububaoboi.py.
    @site: https://phongphongtam2.com/
'''

from urllib.parse import urljoin
import requests, re, os, unicodedata, zipfile, io, time, shutil
import html
import glob # Thêm glob để tìm file xhtml đã lưu
from pathlib import Path # Thêm Path
import os, re, time, html, unicodedata, datetime, zipfile
from pathlib import Path
from typing import Optional, List, Dict, Tuple
import requests
from bs4 import BeautifulSoup, Tag, NavigableString, Comment
# Pillow (cover)
try:
    from PIL import Image
    HAS_PILLOW = True
except Exception:
    HAS_PILLOW = False
    os.system("pip install Pillow")
    from PIL import Image

# =============== CẤU HÌNH ===============
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 1
RETRY_STATUS = {429, 500, 502, 503, 504}
EPUB_TARGET = "epub3"

# Kích thước tối đa cho ảnh bìa (Kobo/e-reader thân thiện)
MAX_COVER_SIZE = (1600, 2400) # (width, height)


# =============== TIỆN ÍCH CHUNG (ĐỒNG BỘ TỪ bububaoboi.py) ===============

def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""

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


def _clean_chapter_title(raw_title: str, chapter_idx: int) -> str:
    """
    Giữ lại logic làm sạch tiêu đề của bububaoboi/giatochotran
    """
    cleaned_title = raw_title or ""
    cleaned_title = re.sub(r"Protected\s*-\s*", "", cleaned_title, flags=re.IGNORECASE)
    cleaned_title = re.sub(r"\[[^\]]+\]", "", cleaned_title).strip()
    m = re.search(r'(chương\s*\d+(\s*[^\d:]*)?)', cleaned_title, re.IGNORECASE)
    return m.group(1).strip().capitalize() if m else f"Chương {chapter_idx}"


def _fetch_html(url: str, password: Optional[str] = None, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    session = requests.Session()
    session.headers.update(HEADERS)
    for k in range(tries):
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff*(k+1)); continue
        if 400 <= r.status_code < 500:
            r.raise_for_status()
        r.raise_for_status()
        break
    else:
        raise requests.exceptions.RequestException(f"Failed to fetch HTML for {url} after {tries} attempts.")
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    # Giữ lại logic xử lý password form (WordPress)
    password_form = soup.find('form', class_='post-password-form')
    if password_form and password:
        post_url = password_form.get('action') or url
        redirect_to_tag = password_form.find('input', {'name': 'redirect_to'})
        redirect_to = redirect_to_tag.get('value') if redirect_to_tag else url
        payload = {'post_password': password, 'Submit': 'Enter', 'redirect_to': redirect_to}
        r_post = session.post(post_url, data=payload, allow_redirects=False, timeout=TIMEOUT)
        if r_post.status_code in (302, 200):
            r_unlocked = session.get(url, timeout=TIMEOUT)
            if not r_unlocked.encoding or r_unlocked.encoding.lower() == "iso-8859-1":
                r_unlocked.encoding = r_unlocked.apparent_encoding
            r_unlocked.raise_for_status()
            return BeautifulSoup(r_unlocked.text, "html.parser")
    return soup


#----------------------INFO TRUYỆN (PHONG PHONG TÂM 2)----------------------#
def _get_book_info(soup: BeautifulSoup) -> dict:
    list_info = {}
    
    # Lấy Title - Author từ div.post-title
    title_node = _text(soup.find("div", class_="post-title")).split("–")
    title = title_node[0].strip() if title_node else "Unknown"
    author = title_node[1].strip() if len(title_node) > 1 else "Unknown"
    list_info['Title'] = title # Đổi key title -> Title để đồng bộ với bububaoboi
    list_info['Author'] = author # Đổi key author -> Author

    items = soup.find_all('div', class_='post-content_item')
    list_info['Genre'] = "N/A" # Đổi key genre -> Genre
    list_info['Status'] = "N/A" # Thêm key Status

    for item in items:
        heading = item.find('div', class_='summary-heading')
        content = item.find('div', class_='summary-content')
        if heading and content:
            if 'Thể loại' in heading.text:
                list_info['Genre'] = content.text.strip()
            elif 'Trạng thái' in heading.text:
                list_info['Status'] = content.text.strip()

    # CoverURL
    image_node = soup.find("div", class_="summary_image")
    list_info['CoverURL'] = "" # Đổi key cover -> CoverURL
    if image_node:
        img_tag = image_node.find('img')
        if img_tag and img_tag.has_attr('src'):
            list_info['CoverURL'] = img_tag['src']

    return list_info


def _get_list_chapters(url: str) -> list:
    """
    Lấy danh sách chương từ API/AJAX của phongphongtam2.com.
    """
    chapters = []
    # URL AJAX để lấy danh sách chương
    url_post = url.rstrip("/") + "/ajax/chapters"
    querystring = {"t": "1"}
    payload = ""
    headers = {
        "accept": "*/*",
        "accept-language": "vi,en-US;q=0.9,en;q=0.8",
        "origin": "https://phongphongtam2.com",
        "referer": url,
        "user-agent": HEADERS["User-Agent"],
        "x-requested-with": "XMLHttpRequest"
    }
    
    # Sử dụng POST request
    response = requests.post(url_post, data=payload, headers=headers, params=querystring)
    if response.status_code == 200:
        # Nội dung trả về là HTML Fragment chứa danh sách chương
        soup = BeautifulSoup(response.text, "html.parser")
        ul_node = soup.find("ul", class_="main version-chap no-volumn")
        if ul_node:
            a_node = ul_node.find_all("a")
            for a in a_node:
                # Đổi key link -> url để đồng bộ với bububaoboi
                chapters.append({"title": _text(a), "url": a.get("href")})
    return chapters


#----------------------BÓC NỘI DUNG CHƯƠNG (HTML sạch)----------------------#
def _get_content_chapter(url: str) -> str:
    """
    Trả về 'clean' HTML body (chuỗi chỉ chứa <p>...) và tiêu đề thô.
    """
    soup = _fetch_html(url)
    
    # Tiêu đề thô của chương
    chapter_title = _text(soup.find("h1", id="chapter-heading")).strip()
    
    content_node = soup.find("div", class_="entry-content")
    if not content_node:
        print(f"⚠ Không tìm thấy nội dung tại {url}")
        return ""

    # Bổ sung logic xóa phần "TOÀN VĂN HOÀN" (MỚI)
    for p in list(content_node.find_all('p')):
        txt = p.get_text(" ", strip=True)
        if 'TOÀN VĂN HOÀN' in txt.upper():
            print(f"    -> Đã loại bỏ phần 'TOÀN VĂN HOÀN'")
            p.decompose() 
            continue 

    # Loại bỏ unwanted <p> (credits/empty)
    for p in list(content_node.find_all('p')):
        txt = p.get_text(" ", strip=True)
        if 'Editor:' in txt or 'Beta:' in txt or txt.strip() == '—' or not txt.strip():
            p.decompose()

    # remove warning divs
    for warning_div in content_node.find_all('div', class_='chapter-warning'):
        warning_div.decompose()

    # remove comments
    for comment in content_node.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # unwrap tags except p, img, br, a, strong, em
    for tag in content_node.find_all():
        if tag.name not in ['p', 'img', 'br', 'a', 'strong', 'b', 'em', 'i', 'span', 'u']:
            try:
                tag.unwrap()
            except Exception:
                pass
                
    # Lấy nội dung HTML đã dọn dẹp, chỉ giữ lại nội dung bên trong <div class="entry-content">
    content_html = "\n".join(str(c) for c in content_node.children)

    # Cố gắng làm sạch thêm các thẻ HTML không hợp lệ mà bs4 không tự dọn
    content_html = re.sub(r'<\/?(html|head|body|title|doctype)[^>]*>', '', content_html, flags=re.IGNORECASE)

    # Gói vào XHTML hợp lệ
    xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi" lang="vi">
  <head>
    <meta charset="utf-8" />
    <title>{chapter_title}</title>
  </head>
  <body>
    <h1>{chapter_title}</h1>
    <section id="chapter" class="chapter-content">
{BeautifulSoup(content_html, "html.parser").prettify()}
    </section>
  </body>
</html>""".strip()

    return xhtml

# =============== GET TOÀN BỘ CHƯƠNG & LƯU FILE ===============
def _get_content_chapters(chap_list: List[Dict[str, str]],
                          book_title: str | None = None,
                          base_output_dir: str | Path = "output") -> List[Tuple[int, str, str]]:
    """
    Tương tự logic bububaoboi: Lấy nội dung tất cả chương và lưu ra file.
    """
    if not chap_list:
        print("⚠ Không có chương nào trong chap_list.")
        return []

    book_dir_name = _slug_folder(book_title or "book") # dùng _slug_folder
    out_dir = ensure_dir(Path(base_output_dir) / book_dir_name)

    total = len(chap_list)
    results: List[Tuple[int, str, str]] = []
    pad = max(3, len(str(total)))

    print(f"\n--- Bắt đầu tải {total} chương ---")
    
    for i, chap in enumerate(chap_list, start=1):
        raw_title = chap.get("title") or f"Chương {i}"
        url   = chap.get("url")
        try:
            # Lấy nội dung XHTML
            xhtml = _get_content_chapter(url)
            
            # Lấy tiêu đề sạch (dùng cho log và EPUB TOC)
            clean_title = _clean_chapter_title(raw_title, i)
            
            # Tên file: 001.xhtml
            fname = f"{str(i).zfill(pad)}.xhtml"
            fpath = out_dir / fname
            
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(xhtml)
            
            # Log sử dụng tiêu đề sạch
            print(f"[{str(i).zfill(pad)}/{str(total).zfill(pad)}] Saved - {clean_title} - {fpath}")
            
            # Trả về clean_title và XHTML
            results.append((i, clean_title, xhtml))
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"[{str(i).zfill(pad)}/{str(total).zfill(pad)}] ❌ Lỗi lấy '{raw_title}' ({url}): {e}")
    return results


# =============== COVER HELPERS (ĐỒNG BỘ TỪ bububaoboi.py) ===============

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
        # Dùng thuộc tính Resampling.LANCZOS nếu có
        resample_filter = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
        
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, resample_filter)
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

# Tạm thời bỏ qua _fetch_cover_from_book_page_bububaoboi vì không cần thiết cho logic chung.

def _load_cover_from_input(cover_in: str, story_url: str, info_cover_url: str):
    """
    Tương tự logic bububaoboi: Ưu tiên input > info_cover_url (tải ảnh)
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

    if data is None and info_cover_url:
        try:
            print(f"Tự động lấy cover từ CoverURL trong get_book_info: {info_cover_url}")
            r = requests.get(info_cover_url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.content
        except Exception as e:
            print(f"⚠ Không tải được CoverURL mặc định: {e}.")

    if data:
        return _ensure_jpeg_cover(data)
    return (None, None, None)


# =============== EPUB3 (Kobo-friendly) (ĐỒNG BỘ TỪ bububaoboi.py) ===============
def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    zipf.writestr(zinfo, data_bytes)

def create_epub_epub3_for_kobo(book_title: str, author: str, items: list,
                               out_epub_dir: str, cover_bytes=None, cover_ext=None, cover_mime=None,
                               language="vi", publisher="Hishiro"):
    """Hàm đóng gói EPUB thủ công, lấy từ bububaoboi.py"""
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
            
            # Lấy nội dung đã làm sạch (chỉ phần body)
            # Hàm _get_content_chapters đã trả về XHTML hoàn chỉnh, cần bóc body
            chap_soup = BeautifulSoup(c.get("xhtml_content") or "", "html.parser")
            # Cố gắng tìm nội dung bên trong <section id="chapter">
            node = chap_soup.find("section", id="chapter")
            if not node: # Fallback: tìm trong body
                node = chap_soup.find("body")
            
            # Tiêu đề H1 (lấy lại từ XHTML đã lưu)
            h1_title = _text(chap_soup.find("h1"))
            
            content_html = "".join(str(c) for c in node.children) if node else ""
            
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
                f'  <div>{content_html or ""}</div>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            
            _epub_write(z, f"OEBPS/{fn}", xhtml)
            
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            toc_entries.append((fn, c.get("title") or f"Chương {i}")) # c['title'] là tiêu đề sạch

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

# =============== MAIN FLOW (ĐIỀU CHỈNH LẠI) ===============
def download_and_build_epub(url_story: str):
    
    # 1. Info
    soup = _fetch_html(url_story)
    info = _get_book_info(soup)
    title  = info.get("Title") or "Truyện"
    author = info.get("Author") or "—"
    cover_auto_url = info.get("CoverURL") or ""

    print("--------------------------Thông tin truyện--------------------------")
    for k, v in info.items():
        print(f"{k}: {v}")

    # 2. Danh sách chương
    chapters = _get_list_chapters(url_story)
    if not chapters:
        print("❌ Không tìm thấy chương nào. Không thể tiếp tục.")
        return
        
    print(f"Total chapters: {len(chapters)}")

    # 3. Cover (Tương tự logic bububaoboi)
    # Vì logic main của bububaoboi.py yêu cầu input, ta giả định bỏ qua input và dùng auto.
    cover_bytes, cover_ext, cover_mime = _load_cover_from_input("", url_story, cover_auto_url)
    print(f"Cover: {'OK' if cover_bytes else 'MISSING'} ({cover_mime or '-'})")

    # 4. Lưu chương XHTML sạch vào output/TênTruyện
    out_dir = os.path.join("output", _slug_folder(title))
    os.makedirs(out_dir, exist_ok=True)
    
    # [(idx, clean_title, xhtml_content)]
    chaps_saved = _get_content_chapters(chapters, book_title=title) 

    # Gom item để build EPUB3
    all_items = []
    for idx, chap_title, xhtml in chaps_saved:
        # Cần gửi cả xhtml_content để hàm create_epub_epub3_for_kobo có thể bóc nội dung bên trong
        all_items.append({"title": chap_title, "xhtml_content": xhtml})

    if not all_items:
        print("❌ Không có chương nào được tải thành công. Không thể tạo EPUB.")
        return

    # 5. Build EPUB3 (publisher = Hishiro) -> output/
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
        publisher="Hishiro"
    )
    print(f"✅ EPUB: {epub_path}")
    print(f"📁 Thư mục chương đã lưu: {out_dir}")

if __name__ == "__main__":
    UrlStory= "https://phongphongtam2.com/manga/tinh-yeu-den-muon-diep-kien-tinh/"
    download_and_build_epub(UrlStory)