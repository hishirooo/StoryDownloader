# -*- coding: utf-8 -*-
'''
    @Author: Hishio - Cá mụp cắn cáp
    @github: https://github.com/hishirooo
    @date: 20-11-2025
    @version: 1.3 - Final fix: Cleaned chapter titles to only show "Chương xxx" for TOC/filename/log.
    @site: giatochotran.wordpress.com
'''


from bs4 import BeautifulSoup, Tag
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, urlunparse, urljoin
import requests, ssl, urllib3, re, json, html, os, unicodedata, zipfile, io, datetime, shutil, time
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import importlib, re, sys, os, io, glob
from download_logger import chapter_log_line
from epub_metadata import PUBLISHER, subject_xml

# Thử import Pillow cho xử lý ảnh bìa
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("⚠ Cảnh báo: Không tìm thấy thư viện Pillow. Không thể tối ưu/chuyển đổi cover sang JPEG.")

# =============== CẤU HÌNH ===============
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}
EPUB_TARGET = "epub3"

# Kích thước tối đa cho ảnh bìa (Kobo/e-reader thân thiện)
MAX_COVER_SIZE = (1600, 2400) # (width, height)

def _text(el) -> str:
    '''
    Lấy text từ thẻ BeautifulSoup, trả về chuỗi rỗng nếu lỗi.
    '''
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""

def _safe_filename(s: str) -> str:
    """Tạo tên file an toàn từ tiêu đề chương."""
    s = s.replace(":", " -").replace("/", " ")
    s = re.sub(r'[\\/*?:"<>|]', "", s)
    return s[:100].strip()

def _slugify_vi(s: str) -> str:
    """Slug ASCII an toàn cho tên thư mục/EPUB (Windows/Kobo thân thiện)."""
    s = (s or "book").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-_")
    return (s or "book")[:80]

def _clean_chapter_title(raw_title: str, chapter_idx: int) -> str:
    """
    Làm sạch tiêu đề, chỉ giữ lại định dạng 'Chương xxx'.
    """
    cleaned_title = raw_title
    
    # 1. Loại bỏ tiền tố Protected và mã truyện ([CMĐVPL]...)
    cleaned_title = re.sub(r"Protected\s*-\s*", "", cleaned_title, flags=re.IGNORECASE)
    cleaned_title = re.sub(r"\[[^\]]+\]", "", cleaned_title).strip()

    # 2. Đảm bảo chỉ còn lại "Chương xxx" (hoặc "Chương xxx: Tên")
    m = re.search(r'(chương\s*\d+(\s*[^\d]*)?)', cleaned_title, re.IGNORECASE)
    
    # Nếu tìm thấy cụm "Chương xxx" thì trả về nó, ngược lại dùng fallback
    return m.group(1).strip() if m else f"Chương {chapter_idx}"


def _fetch_html(url: str, password: Optional[str] = None, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    '''
    Tải HTML, xử lý form mật khẩu nếu có.
    '''
    session = requests.Session()
    session.headers.update(HEADERS)
    
    # Lần tải đầu tiên
    for k in range(tries):
        r = session.get(url, timeout=TIMEOUT)
        
        # Xử lý retry tương tự như logic cũ
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status() 
            time.sleep(backoff*(k+1)); continue
        if 400 <= r.status_code < 500:
            r.raise_for_status() 
        r.raise_for_status()
        break
    else:
        # Nếu vòng lặp kết thúc mà không break (tức là request lỗi liên tục)
        raise requests.exceptions.RequestException(f"Failed to fetch HTML for {url} after {tries} attempts.")
        
    # Chuẩn bị để decode
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding

    soup = BeautifulSoup(r.text, "html.parser")
    
    # KIỂM TRA FORM MẬT KHẨU
    password_form = soup.find('form', class_='post-password-form')
    if password_form and password:
        print("🔐 Phát hiện chương được bảo vệ bằng mật khẩu. Đang thử mở khóa...")
        
        # Lấy action URL cho POST request (thường là URL hiện tại hoặc /wp-login.php?action=postpass)
        post_url = password_form.get('action')
        if not post_url:
            post_url = url
            
        # Lấy redirect_to (giá trị ẩn)
        redirect_to_tag = password_form.find('input', {'name': 'redirect_to'})
        redirect_to = redirect_to_tag.get('value') if redirect_to_tag else url

        # Dữ liệu form cần gửi
        payload = {
            'post_password': password,
            'Submit': 'Enter',
            'redirect_to': redirect_to
        }
        
        # Gửi POST request để mở khóa
        r_post = session.post(post_url, data=payload, allow_redirects=False, timeout=TIMEOUT)

        # WordPress thường trả về 302 (Redirect) và Cookies (wordpress_post_pass) được set.
        # Bây giờ ta cần tải lại URL gốc với session đã được set cookies.
        if r_post.status_code == 302 or r_post.status_code == 200:
            print("🔓 Mở khóa thành công. Đang tải lại nội dung chương...")
            # Tải lại nội dung chương bằng session đã có cookie mở khóa
            r_unlocked = session.get(url, timeout=TIMEOUT)
            
            if not r_unlocked.encoding or r_unlocked.encoding.lower() == "iso-8859-1":
                r_unlocked.encoding = r_unlocked.apparent_encoding
            r_unlocked.raise_for_status()
            
            return BeautifulSoup(r_unlocked.text, "html.parser")
        else:
            print(f"❌ Lỗi khi gửi mật khẩu: Status code {r_post.status_code}")
            # Vẫn trả về soup cũ (chứa form mật khẩu) để hàm gọi thấy rằng không có nội dung.
            pass
            
    return soup

#----------------------INFO TRUYỆN----------------------#

def _get_book_info(soup: BeautifulSoup) -> dict:
    list_info = {}  

    # Tên truyện từ trang mục lục (thường là entry-title của post)
    title_tag = soup.find("h1", class_="entry-title")
    title = _text(title_tag) if title_tag else "Truyện Không Tên"
    
    # Tìm thông tin Tác giả và Thể loại
    author = "Không rõ"
    genre = "Không rõ"

    for p in soup.find_all('p', style=lambda value: value and 'text-align: center' in value):
        p_text = p.get_text(strip=True)
        if 'Tác giả:' in p_text:
            author = p_text.replace('Tác giả:', '').strip()
        if 'Thể loại:' in p_text:
            m = re.search(r'Thể loại:\s*(.*?)(\.|$|\()', p_text)
            if m:
                 genre = m.group(1).strip()
            else:
                 genre = p_text.replace('Thể loại:', '').strip()
            genre = re.sub(r'\s*\([^)]*\)$', '', genre).strip()
            

    # Ảnh bìa - tìm trong div#NEN-ANH (cấu trúc dựa trên file gốc)
    cover = None
    div = soup.find("div", {"id": "NEN-ANH"})
    if div:
        style = div.get("style")
        # Dùng regex để trích xuất URL trong url('...')
        match = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
        if match:
            cover = match.group(1)
    
    list_info["Cover"] = cover
    list_info["Title"] = title
    list_info["Author"] = author
    list_info["Genre"] = genre
    return list_info
        
# Lấy ảnh bìa truyện
def _fetch_cover_content(url: str):
    '''Lấy ảnh bìa từ URL. Trả về (nội dung ảnh bytes, đuôi mở rộng, url ảnh) hoặc (None, None, None).'''
    if not url: return None, None, None
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        content = r.content
        ct = r.headers.get("Content-Type","").lower()
        ext = ".png" if ("png" in ct or url.lower().endswith(".png")) else ".jpg"
        return content, ext, url
    except Exception as e:
        print(f"⚠ Lỗi khi tải cover từ {url}: {e}")
        return None, None, None

def _sniff_image_type(data: bytes) -> tuple[str, str]:
    """
    Đoán định dạng ảnh từ header.
    Trả về (ext, mime). Nếu không biết: ('.bin', 'application/octet-stream')
    """
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

def _ensure_jpeg_cover(img_bytes: bytes) -> tuple[bytes, str, str]:
    """
    Chuyển ảnh bất kỳ về JPEG (RGB, quality=90). 
    Đã bổ sung resize để tối ưu cho máy đọc sách (Kobo).
    Trả về (bytes, '.jpg', 'image/jpeg').
    Nếu không có Pillow hoặc convert lỗi → trả về ảnh gốc + mime sniffed.
    """
    if not HAS_PILLOW:
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)
        
    try:
        import io as _io
        im = Image.open(_io.BytesIO(img_bytes))
        
        # 1. Resize/Resample để tối ưu cho e-reader
        # Chỉ resize nếu một trong hai cạnh vượt quá giới hạn
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
             im.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
             print(f"✔ Cover resized to: {im.size[0]}x{im.size[1]} (max: {MAX_COVER_SIZE[0]}x{MAX_COVER_SIZE[1]})")
        
        # 2. Convert sang RGB (loại bỏ alpha layer)
        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
            
        # 3. Save as JPEG
        out = _io.BytesIO()
        im.save(out, format="JPEG", quality=90, optimize=True)
        return (out.getvalue(), ".jpg", "image/jpeg")
        
    except Exception as e:
        # Fallback: giữ nguyên dữ liệu gốc
        ext, mime = _sniff_image_type(img_bytes)
        print(f"⚠ Không convert/resize được cover sang JPEG: {e}. Dùng ảnh gốc {ext}.")
        return (img_bytes, ext, mime)
        
        
        
def _get_list_chapters(soup) -> list:
    list_chapters = []    
    
    # Bước 1: Tìm tất cả các bảng (<table>) chứa danh sách chương.
    # Ta tìm các thẻ <div> chứa các bảng, vì chúng có style chung cho khu vực mục lục.
    muc_luc_divs = soup.find_all('div', style=lambda value: value and 'font-family: roboto;' in value)

    # Lặp qua các div này và tìm các thẻ <a> bên trong
    for div in muc_luc_divs:
        # Lọc các khối div mục lục có chứa <table>
        if not div.find('table'):
            continue
            
        # Bước 2: Tìm tất cả các thẻ liên kết (<a>) trong khu vực mục lục
        links = div.find_all('a')
        
        for link in links:
            # Lấy tiêu đề chương.
            tieu_de_raw = link.get_text(strip=True)
            duong_dan = link.get('href')

            # Lọc: chỉ giữ liên kết có chứa "Chương" và không phải là liên kết rỗng
            if duong_dan and 'chương' in tieu_de_raw.lower() and not tieu_de_raw.lower().startswith('xxx'):
                list_chapters.append({
                    'tieu_de': tieu_de_raw,
                    'duong_dan': duong_dan
                })

    return list_chapters

    
def _get_chapter_content_html(url: str, password: Optional[str] = None) -> Tuple[str, str]: 
    """
    Trích xuất nội dung chính của chương từ HTML, xử lý mật khẩu nếu cần, và định dạng thành HTML content.
    
    Returns: (title, content_html_string)
    """
    # Truyền password vào _fetch_html
    soup = _fetch_html(url, password=password)
    
    # 1. Lấy Tiêu đề chương
    title_tag = soup.find("h1", class_="entry-title")
    title_chapter = _text(title_tag) if title_tag else "Không tìm thấy tiêu đề"
    
    # KIỂM TRA XEM CÓ CÒN FORM MẬT KHẨU HAY KHÔNG
    if soup.find('form', class_='post-password-form'):
        return f"Protected - {title_chapter}", "<p>🚨 Chương vẫn bị khóa hoặc mật khẩu không hợp lệ. Không thể trích xuất nội dung.</p>"

    div_content = soup.find("div", class_="entry-content")
    
    if not div_content:
        return title_chapter, "<p>(Không tìm thấy nội dung)</p>"

    content_html_list = []
    
    # 2. Tìm đến div chứa nội dung chính
    content_area = div_content.find('div', style=lambda value: value and 'font-family: roboto;' in value)
    
    if not content_area:
        # Nếu không tìm thấy div bọc nội dung (content_area), thử lấy trực tiếp từ div_content
        content_area = div_content 
    
    # Lấy tất cả các thẻ con (p, div, h...) trong khu vực nội dung
    for element in content_area.children:
        # Chỉ xử lý các thẻ (Tag) và là các thẻ chứa văn bản chính
        if isinstance(element, Tag) and element.name in ('p', 'div', 'h2', 'h3', 'h4', 'h5', 'strong'):
            text = _text(element)
            
            # Loại trừ các nút điều hướng ở cuối chương
            is_navigation_p = element.name == 'p' and element.get('style') and 'text-align: center' in element.get('style')
            if is_navigation_p and 'MỤC LỤC' in text:
                continue
            
            # Loại bỏ dòng 'Hết chương X'
            if text and text.strip().lower().startswith('hết chương'):
                continue
            
            if text:
                # Loại bỏ các ký tự dấu * đứng một mình
                if text == '*':
                    continue
                    
                # Tạo HTML cho đoạn văn
                if element.name == 'p':
                    # Lấy nội dung HTML bên trong thẻ p (bao gồm cả span, strong, a)
                    inner_html = "".join(str(x) for x in element.contents if x and _text(x) or x.name in ('br', 'img'))
                    if inner_html.strip():
                        content_html_list.append(f"<p>{inner_html}</p>")
                else:
                    # Nếu là các thẻ khác (div, h), wrap thành div/h cho an toàn
                    content_html_list.append(str(element))
    
    content_html_final = "".join(content_html_list)
    if not content_html_final:
         return title_chapter, "<p>(Không có nội dung)</p>"
         
    return title_chapter, content_html_final

def fetch_chapter_content(url: str, password: Optional[str] = None) -> dict:
    '''Wrapper trả về dict cho hàm tạo EPUB/HTML'''
    title, content_html = _get_chapter_content_html(url, password=password)
    return {"title": title, "content_html": content_html, "url": url}


XHTML_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{doc_title}</title>
  <link href="../Styles/style.css" rel="stylesheet" type="text/css"/>
</head>
<body>
  <h1>{chapter_title}</h1>
  <div class="meta">{book_title}</div>
  <div class="content">
  {content}
  </div>
</body>
</html>"""

def save_chapter_xhtml(book_title: str, chapter_idx: int, chap: dict, out_dir: str) -> str:
    '''Lưu chương dưới dạng file XHTML. Trả về đường dẫn file đã lưu.'''
    os.makedirs(out_dir, exist_ok=True)
    
    # Lấy tiêu đề chương thô
    raw_title = chap.get('title') or f'Chuong {chapter_idx}'
    
    # --- LOGIC QUAN TRỌNG: CHUẨN HÓA TIÊU ĐỀ CHO MỤC LỤC, LOG VÀ TÊN FILE ---
    # Tiêu đề sạch chỉ giữ lại "Chương xxx"
    display_title = _clean_chapter_title(raw_title, chapter_idx)

    # Tên file vật lý chỉ dùng display_title sạch
    fname = f"{chapter_idx:04d} - {_safe_filename(display_title)}.xhtml"
    path  = os.path.join(out_dir, fname)
    
    # Dùng template tối giản cho XHTML. 
    # Tiêu đề trong file XHTML (<h1>) giữ nguyên raw_title (có thể có 'Protected - [...]')
    xhtml_out = XHTML_TEMPLATE.format(
        doc_title     = f"{book_title} - {raw_title}",
        chapter_title = html.escape(raw_title), 
        book_title    = html.escape(book_title or "Truyện"),
        src           = chap.get("url") or "",
        content       = chap.get("content_html") or "<p>(Không có nội dung)</p>",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(xhtml_out)
        
    # Thêm display_title vào dict để dùng cho Log và create_epub
    chap['display_title'] = display_title
    return path

def save_all_chapters_to_xhtml(book_title: str, chapters: list, out_dir: str,
                              pass_unlock: Optional[str] = None, 
                              start: int = 1, end: Optional[int] = None):
    '''Lưu tất cả chương thành file XHTML.
    Trả về danh sách đường dẫn file đã lưu.
    '''
    n = len(chapters)
    if end is None or end > n: end = n
    saved = []
    
    # Tạo thư mục con "Text" cho các file xhtml theo chuẩn EPUB 
    chap_out_dir = os.path.join(out_dir, "Text")
    os.makedirs(chap_out_dir, exist_ok=True)
    
    print(f"\n--- Bắt đầu tải {end - start + 1} chương ({start} đến {end}) ---")
    
    selected_total = end - start + 1
    for done, i in enumerate(range(start, end + 1), 1):
        info = chapters[i-1]
        try:
            chap_title, chap_url = info.get('tieu_de', f'Chương {i}'), info.get('duong_dan')
            
            # Fetch nội dung
            c = fetch_chapter_content(chap_url, password=pass_unlock)
            
            if not c.get("title"): 
                c["title"] = chap_title
            
            # Kiểm tra xem chương có bị khóa không
            if c.get("title", "").startswith("Protected - "):
                 print(chapter_log_line(done, selected_total, "ERR", i, n, f"{chap_title} bị khóa"))
            
            # Lưu file, c['display_title'] sẽ được tạo trong hàm này
            p = save_chapter_xhtml(book_title, i, c, chap_out_dir)
            
            # Log format chung: [XXX/YYY] [HTTP=...] Chương XXXX/YYYY: Tên chương
            # Dùng display_title đã được làm sạch
            print(chapter_log_line(done, selected_total, c.get("status_code", 200), i, n, c["display_title"]), flush=True)
            saved.append(p)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(chapter_log_line(done, selected_total, "ERR", i, n, f"{chap_title} ({e})"), flush=True)
    return saved


def _epub_write(zipf, arcname, data_bytes, compress=True):
    '''
    Ghi dữ liệu vào file nén EPUB.
    '''
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)

def create_epub(book_url: str | None,
                book_title: str,
                author: str,
                chapters: list,            # list[str path_html]
                out_epub_path: str,        # đường dẫn .epub
                creator: str = "Hishiro",
                language: str = "vi",
                epub_target: str | None = None,
                cover_bytes: bytes | None = None,
                cover_ext: str | None = None,
                cover_mime: str | None = None,
                tags=None) -> str:
    """
    Tạo EPUB2 hoặc EPUB3 từ danh sách đường dẫn file XHTML đã lưu. 
    """
    book_title = book_title or "Truyện"
    author     = author or "—"
    target     = (epub_target or EPUB_TARGET).lower().strip()

    # 0) Output path
    out_is_dir = (os.path.isdir(out_epub_path) or not out_epub_path.lower().endswith(".epub"))
    if out_is_dir:
        os.makedirs(out_epub_path, exist_ok=True)
        out_file = os.path.join(out_epub_path, f"{_slugify_vi(book_title)}.epub")
    else:
        os.makedirs(os.path.dirname(out_epub_path) or ".", exist_ok=True)
        out_file = out_epub_path

    # 1) Thu thập nội dung chương (Đọc từ XHTML cục bộ)
    items = []
    # chapters ở đây là list[str path_to_xhtml]
    for i, html_path in enumerate(chapters, 1):
        try:
            print(f"Readfile : {os.path.basename(html_path)} from {os.path.dirname(os.path.dirname(html_path))}")
            
            with open(html_path, "r", encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), "html.parser")
            
            # Lấy tiêu đề file SẠCH (Chương xxx)
            base = os.path.splitext(os.path.basename(html_path))[0]
            m = re.match(r"^\s*(\d+)\s*-\s*(.+)$", base)
            display_title = m.group(2).strip() if m else base

            # Lấy tiêu đề THÔ (Protected - [CMĐVPL] Chương 8) từ thẻ H1/TITLE để hiển thị nội dung
            title_node = soup.find("h1") or soup.find("title")
            raw_title = _text(title_node)
            
            # Lấy nội dung trong thẻ div.content
            content_node = soup.select_one("div.content") or soup.body or soup
            content_html = "".join(str(x) for x in content_node.contents)

            # SỬ DỤNG DISPLAY_TITLE SẠCH CHO MỤC LỤC
            items.append({
                "title": display_title,  
                "content_html": content_html, 
                "source_path": html_path,
                "raw_title": raw_title # Tiêu đề thô cho H1 bên trong XHTML
            })
        except Exception as e:
            print(f"WARN đọc XHTML '{html_path}': {e}")
            
    if not items:
        raise ValueError("Không có nội dung chương nào hợp lệ để tạo EPUB.")


    # 2) Cover: 
    if cover_bytes is not None and (not cover_ext or not cover_mime):
        cover_ext, cover_mime = _sniff_image_type(cover_bytes)

    cover_relpath = f"Images/cover{cover_ext}" if (cover_bytes and cover_ext) else None

    # 3) Đóng EPUB
    with zipfile.ZipFile(out_file, "w") as z:
        # 3.1 mimetype
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        # 3.2 container.xml
        container_xml = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<container version=\"1.0\" xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">\n"
            "  <rootfiles>\n"
            "    <rootfile full-path=\"OEBPS/content.opf\" media-type=\"application/oebps-package+xml\"/>\n"
            "  </rootfiles>\n"
            "</container>"
        ).encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)
        
        # Thêm file cover.xhtml cho EPUB2/3 
        if cover_relpath:
            cover_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi" xmlns:epub="http://www.idpf.org/2007/ops">\n'
                '<head>\n'
                '  <title>Cover</title>\n'
                '  <meta charset="utf-8"/>\n'
                '  <style type="text/css">.cover{height:100vh;max-width:100%;object-fit:contain;margin:0 auto;display:block}</style>\n'
                f'  <link href="../Styles/style.css" rel="stylesheet" type="text/css"/>\n'
                '</head>\n'
                '<body>\n'
                f'  <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="100%" height="100%" viewBox="0 0 1000 1500" preserveAspectRatio="xMidYMid meet" epub:type="cover">\n'
                f'    <image width="1000" height="1500" xlink:href="../{cover_relpath}" class="cover"/>\n'
                f'  </svg>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/Text/cover.xhtml", cover_xhtml)

        # 3.3 Styles
        style_css = (
            "body{font-family:serif;line-height:1.6}"
            "img{max-width:100%;height:auto}"
            "h1{font-size:1.4em;margin:0 0 .6em}"
        )
        _epub_write(z, "OEBPS/Styles/style.css", style_css.encode("utf-8"))

        # 3.4 Chapters
        manifest_items, spine_items, navpoints = [], [], []
        chap_refs = [] 
        
        # Thêm cover page vào spine đầu tiên
        if cover_relpath:
            manifest_items.append('<item id="cover-html" href="Text/cover.xhtml" media-type="application/xhtml+xml" properties="svg"/>')
            spine_items.append('<itemref idref="cover-html" linear="no"/>')
            
        # Thêm cover media vào manifest
        if cover_relpath:
             manifest_items.append(f'<item id="cover-img" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}" properties="cover-image"/>')

        for i, c in enumerate(items, 1):
            # Lấy tên file đã lưu (ví dụ: 0001 - Ten chuong.xhtml)
            fn_base = os.path.basename(c["source_path"])
            fn = f"Text/{fn_base}"
            
            # Ghi nội dung XHTML đã đọc từ file vào EPUB
            chapter_xhtml_content = XHTML_TEMPLATE.format(
                doc_title     = f"{book_title} - {c['raw_title']}",
                chapter_title = html.escape(c['raw_title']), # Dùng tiêu đề THÔ cho H1
                book_title    = html.escape(book_title or "Truyện"),
                src           = c.get("url") or "",
                content       = c['content_html']
            ).encode("utf-8")
            
            _epub_write(z, f"OEBPS/{fn}", chapter_xhtml_content)
            
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            navpoints.append(
                f'<navPoint id="navPoint-{i}" playOrder="{i}" class="chapter">'
                f'<navLabel><text>{html.escape(c["title"])}</text></navLabel>' # Dùng c['title'] SẠCH cho mục lục
                f'<content src="{fn}"/></navPoint>'
            )
            chap_refs.append((fn, c["title"])) # Dùng c['title'] SẠCH cho mục lục


        # 3.5 Cover Media
        if cover_relpath and cover_bytes:
            _epub_write(z, f"OEBPS/{cover_relpath}", cover_bytes) # Lưu ảnh bìa

        # 3.6 OPF + NCX / NAV
        manifest_items_str = "\n    ".join(manifest_items)
        spine_items_str    = "\n    ".join(spine_items)
        navpoints_str      = "\n    ".join(navpoints)
        dt_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        subjects = subject_xml(tags, indent="    ")
        
        if target == "epub3":
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
                "\n".join(f'      <li><a href="{fn.split("/", 1)[-1]}">{html.escape(t)}</a></li>' for fn, t in chap_refs) +
                '\n    </ol>\n'
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
                f'    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>\n'
                f'{subjects}'
                f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
                '  </metadata>\n'
                '  <manifest>\n'
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

        else: # EPUB2
            # ... (Logic EPUB2 giữ nguyên) ...
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
                f'    <dc:publisher>{html.escape(PUBLISHER)}</dc:publisher>\n'
                f'{subjects}'
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


    print(f"\n✨ Đã tạo EPUB thành công: {out_file}")
    return out_file

def build_epub_from_files(title: str, author: str, out_epub_path: str, chap_dir: str,
               cover_bytes: bytes | None = None, cover_ext: str | None = None, cover_mime: str | None = None,
               epub_target: str | None = None, tags=None) -> str:
    """
    Quét các file .xhtml trong thư mục Text của chap_dir, sắp xếp, rồi gọi create_epub.
    """
    search_path = os.path.join(chap_dir, "Text", "*.xhtml")
    paths = sorted(glob.glob(search_path))
    
    # Định nghĩa lại hàm sắp xếp
    def _sort_key(p: str):
        base = os.path.splitext(os.path.basename(p))[0]
        m = re.match(r"^\s*(\d+)", base)
        idx = int(m.group(1)) if m else 10**9
        return (idx, base.casefold())
    paths.sort(key=_sort_key)
    
    if not paths:
        raise ValueError(f"Không có file XHTML hợp lệ trong thư mục {os.path.join(chap_dir, 'Text')}")

    return create_epub(
        book_url=None,
        book_title=title,
        author=author,
        chapters=paths, # Truyen danh sach duong dan file
        out_epub_path=out_epub_path,
        creator="Hishiro",
        language="vi",
        epub_target=epub_target or EPUB_TARGET,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime,
        tags=tags,
    )

def download_and_build_epub(url_story: str, pass_unlock_chapter: Optional[str] = None):
    """
    Quy trình chính: Lấy info -> Lấy list chương -> Tải cover -> Tải chương -> Tạo EPUB.
    """
    # 1. Lấy thông tin truyện và danh sách chương
    try:
        soup = _fetch_html(url_story)
        info = _get_book_info(soup)
        chapters = _get_list_chapters(soup)
    except Exception as e:
        print(f"❌ Lỗi khi tải thông tin truyện hoặc danh sách chương: {e}")
        return
        
    title = info.get("Title")
    author = info.get("Author")
    cover_url = info.get("Cover")

    if not title or not chapters:
        print("❌ Lỗi: Không thể lấy thông tin truyện hoặc danh sách chương.")
        return

    print("-----------------------------INFO TRUYỆN-----------------------------")
    print(f"Title : {title}")
    print(f"Author: {author}")
    print(f"Genre : {info.get('Genre')}")
    print(f"Cover : {cover_url or 'Không tìm thấy'}")
    print(f"Total Chapters: {len(chapters)}")
    print("-------------------------------------------------------------------")

    # 2. Cấu hình thư mục đầu ra
    story_slug = _slugify_vi(title)
    # Thư mục truyện là Output/Tên_Truyện/
    story_out_dir = os.path.join("Output", story_slug)
    # Vị trí EPUB là Output/Ten_Truyen.epub
    epub_out_path = os.path.join("Output", f"{story_slug}.epub")
    
    # 3. Tải và xử lý Cover
    cover_bytes, cover_ext, cover_mime = None, None, None
    if cover_url:
        cb, ext, _ = _fetch_cover_content(cover_url)
        if cb:
            cover_bytes, cover_ext, cover_mime = _ensure_jpeg_cover(cb)
            
    # 4. Tải và lưu các chương dưới dạng XHTML
    saved_paths = save_all_chapters_to_xhtml(
        book_title=title, 
        chapters=chapters, 
        out_dir=story_out_dir, 
        pass_unlock=pass_unlock_chapter
    )

    if not saved_paths:
        print("❌ Không có chương nào được tải thành công. Không thể tạo EPUB.")
        return

    # 5. Tạo EPUB từ các file XHTML đã tải
    print(f"\n--- Bắt đầu tạo EPUB từ {len(saved_paths)} file XHTML ---")
    
    try:
        build_epub_from_files(
            title=title,
            author=author,
            out_epub_path=epub_out_path, 
            chap_dir=story_out_dir,
            cover_bytes=cover_bytes,
            cover_ext=cover_ext,
            cover_mime=cover_mime,
            tags=info.get("Genre"),
        )
    except ValueError as e:
        print(f"❌ Lỗi tạo EPUB: {e}")
        

#-----------------------CHẠY CHƯƠNG TRÌNH-----------------------#


try:
    from download_policy import install_adapter_policy as _install_adapter_policy
    _install_adapter_policy(globals())
except Exception:
    pass
if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1:
        from adapter_cli import run_adapter_cli as _run_adapter_cli
        _run_adapter_cli(_sys.modules[__name__], default_url=globals().get("DEFAULT_URL", ""))
        raise SystemExit
    # Cấu hình của bạn
    UrlStory= "https://giatochotran.wordpress.com/2021/12/16/muc-luc-chao-mung-den-voi-phong-phat-song-bong-de/"
    # Thay 'adudu' bằng mật khẩu chính xác nếu cần (nếu không cần thì để None)
    pass_unlock_chapter = "adudu" 
    
    download_and_build_epub(UrlStory, pass_unlock_chapter)
