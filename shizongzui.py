from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, urlunparse, urljoin
import ssl, urllib3, re, json, html, os, unicodedata, zipfile, io, datetime, shutil, time
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
USE_CURL_CFFI = False
try:
    from curl_cffi import requests as curl_requests
    requests = curl_requests
    USE_CURL_CFFI = True
except ImportError:
    try:
        import requests
    except ImportError:
        os.system("pip install curl_cffi")
        from curl_cffi import requests as curl_requests
        requests = curl_requests
        USE_CURL_CFFI = True
import importlib, sys, glob
from uuid import uuid4
# Thử import Pillow cho xử lý ảnh bìa
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("⚠ Cảnh báo: Không tìm thấy thư viện Pillow. Không thể tối ưu/chuyển đổi cover sang JPEG.")

# =============== CẤU HÌNH ===============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 2
SLEEP_BETWEEN_CHAPS = 2
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

def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    '''
    Tải HTML (có chuyển domain + fallback verify=False khi SSLError).
    '''
    for k in range(tries):
        if k > 0:
            time.sleep(SLEEP_BETWEEN_PAGES)
        # Dùng impersonate='chrome110' khi curl_cffi hỗ trợ
        kwargs = {"headers": HEADERS, "timeout": TIMEOUT}
        if USE_CURL_CFFI:
            kwargs["impersonate"] = "chrome110"
        try:
            r = requests.get(url, **kwargs)
        except TypeError as e:
            if USE_CURL_CFFI and "impersonate" in str(e).lower():
                kwargs.pop("impersonate", None)
                r = requests.get(url, **kwargs)
            else:
                raise
        
        # Chỉ retry các lỗi server (5xx) và 429
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status() # Lần cuối mà vẫn lỗi thì raise
            time.sleep(backoff*(k+1)); continue
        
        # Nếu là lỗi client (4xx) khác 429, dừng retry và raise
        if 400 <= r.status_code < 500:
            r.raise_for_status() 

        r.raise_for_status() # Các status code 2xx thành công
        
        # requests trong curl_cffi có thể không có apparent_encoding, nên ta bypass lỗi này nếu gặp
        encoding = r.encoding if hasattr(r, 'encoding') else 'utf-8'
        if not encoding or encoding.lower() == "iso-8859-1":
            encoding = getattr(r, 'apparent_encoding', 'utf-8')
        
        return BeautifulSoup(r.text if getattr(r, 'text', None) else r.content.decode(encoding, 'ignore'), "html.parser")

#----------------------INFO TRUYỆN----------------------
def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    info = {
        "title": "Unknown",
        "author": "Unknown",
        "cover_url": ""
    }

    div_info = soup.find("div", class_="jumbotron masthead")
    # <h1 _msttexthash="8545693" _msthash="10">Mười tội lỗi chết người</h1>
    
    h1 = div_info.find("h1")
    if h1: info["title"] = _text(h1)

    author = div_info.find("p")
    if author: info["author"] = _text(author).split("：")[-1].strip() # Tách lấy tên tác giả sau dấu "："


    return info

def _get_chapter_list(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    chapters = []
    #row
    listchaps = soup.find_all("li", class_="span3")
    for chap in listchaps:
        a = chap.find("a")
        if a and a.get("href"):
            chap_title = _text(a)
            chap_url = urljoin(Url, a["href"])
            chapters.append((chap_title, chap_url))

    return chapters

def _get_chapter_title(soup: BeautifulSoup) -> str:
    '''
    Lấy tiêu đề chương từ BeautifulSoup object.
    Tìm trong <h2> trong <div class="page-header">
    '''
    page_header = soup.find("div", class_="page-header")
    if page_header:
        h2 = page_header.find("h2")
        if h2:
            return _text(h2)
    return "Chương"

def _get_chapter_content(soup: BeautifulSoup, url: str = "") -> Dict[str, str]:
    '''
    Trích xuất nội dung + tiêu đề chương từ BeautifulSoup object.
    Nếu chương có phân trang, sẽ fetch và merge tất cả các trang.
    Loại bỏ: quảng cáo, breadcrumb, phân trang, liên kết chương.
    Return: {"title": str, "content_html": str}
    '''
    from copy import deepcopy
    
    # Hàm xử lý nội dung 1 trang
    def _clean_page_content(page_soup: BeautifulSoup) -> str:
        content_div = page_soup.find("div", class_="span12")
        if not content_div:
            return ""
        
        # Clone để không làm mất dữ liệu gốc
        content_div = deepcopy(content_div)
        
        # Xóa breadcrumb
        for elem in list(content_div.find_all("ul", class_="breadcrumb")):
            elem.decompose()
        
        # Xóa page-header
        for elem in list(content_div.find_all("div", class_="page-header")):
            elem.decompose()
        
        # Xóa pagination
        for elem in list(content_div.find_all("div", class_="pagination")):
            elem.decompose()
        
        # Xóa quảng cáo - convert thành list để tránh lỗi khi decompose
        for elem in list(content_div.find_all("div")):
            if elem is None:
                continue
            # Kiểm tra class="autoads"
            if elem.get("class") and any(cls in ["autoads"] for cls in elem.get("class", [])):
                elem.decompose()
                continue
            # Xử lý typo: calss="autoads"
            if elem.get("calss") and "autoads" in elem.get("calss", ""):
                elem.decompose()
        
        # Xóa các <p> chứa chỉ liên kết chương
        for p in list(content_div.find_all("p")):
            if p is None:
                continue
            text = _text(p).strip()
            if text.startswith("上一篇") or text.startswith("下一篇"):
                p.decompose()
        
        # Lấy nội dung sạch
        return str(content_div).strip()
    
    # Lấy tiêu đề chương
    chapter_title = _get_chapter_title(soup)
    
    # Lấy nội dung trang đầu tiên
    all_content = _clean_page_content(soup)
    
    # ========== XỬ LÝ PHÂN TRANG ==========
    if url:
        # Tìm tất cả links phân trang
        pagination = soup.find("div", class_="pagination")
        if pagination:
            # Lấy tất cả links trong pagination
            page_links = pagination.find_all("a")
            page_urls = []
            
            for link in page_links:
                href = link.get("href", "")
                # Bỏ qua link "current page" (javascript:void(0))
                if href and not href.startswith("javascript"):
                    # Chuyển URL tương đối thành tuyệt đối
                    full_url = urljoin(url, href)
                    page_urls.append(full_url)
            
            # Fetch các trang khác (tránh duplicate)
            seen_urls = {url}
            for page_url in page_urls:
                if page_url not in seen_urls:
                    try:
                        page_soup = _fetch_html(page_url)
                        page_content = _clean_page_content(page_soup)
                        if page_content:
                            all_content += "\n" + page_content
                        seen_urls.add(page_url)
                    except Exception as e:
                        print(f"⚠ Lỗi fetch trang {page_url}: {e}")
                        continue
    
    return all_content


# def _get_all_content(chapters: List[Tuple[str, str]]) -> List[Dict[str, str]]:
#     '''
#     Duyệt qua tất cả chương, fetch và trích xuất nội dung.
#     Return: List[{"title": str, "content_html": str}]
    
#     '''
#     all_chapters_content = []
#     for idx, (chap_title, chap_url) in enumerate(chapters, 1):
#         print(f"Đang xử lý chương {idx}/{len(chapters)}: {chap_title}")
#         try:
#             chap_soup = _fetch_html(chap_url)
#             chap_content = _get_chapter_content(chap_soup, url=chap_url)
#             all_chapters_content.append({
#                 "title": chap_title,
#                 "content_html": chap_content
#             })
#             time.sleep(SLEEP_BETWEEN_CHAPS)
#         except Exception as e:
#             print(f"⚠ Lỗi xử lý chương {chap_title} ({chap_url}): {e}")
#             continue
#     return all_chapters_content

def _save_file_content(filename: str, content: str):
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
        

def _safe_filename(name: str) -> str:
    '''Làm sạch tên để dùng làm tên thư mục/file trên Windows, không cắt bỏ Unicode'''
    # Xóa các ký tự cấm của Windows
    safe_name = re.sub(r'[\\/*?:"<>|]', "", name)
    # Cực kỳ quan trọng: Xóa khoảng trắng và dấu chấm ở đầu/cuối
    return safe_name.strip(". ")

def _slugify(value: str) -> str:
    '''Chuyển chuỗi thành slug nhưng GIỮ LẠI Unicode (tiếng Trung/Việt)'''
    value = re.sub(r'[\\/*?:"<>|]', "", value)
    return re.sub(r'[-\s]+', '-', value).strip(' -')


def _download_cover(cover_url: str) -> Tuple[bytes, str]:
    if not cover_url: return None, None
    try:
        kwargs = {"headers": HEADERS, "timeout": TIMEOUT}
        if USE_CURL_CFFI:
            kwargs["impersonate"] = "chrome110"
        try:
            r = requests.get(cover_url, **kwargs)
        except TypeError as e:
            if USE_CURL_CFFI and "impersonate" in str(e).lower():
                kwargs.pop("impersonate", None)
                r = requests.get(cover_url, **kwargs)
            else:
                raise
        r.raise_for_status()
        content = r.content
        ext = ".jpg"  # mặc định
        if "png" in r.headers.get("content-type", "").lower(): ext = ".png"
        elif "gif" in r.headers.get("content-type", "").lower(): ext = ".gif"
        elif "webp" in r.headers.get("content-type", "").lower(): ext = ".webp"
        return content, ext
    except Exception as e:
        print(f"Lỗi tải cover: {e}")
        return None, None

# Hàm resize cover nếu có Pillow
def _resize_cover(content: bytes, ext: str) -> Tuple[bytes, str]:
    if not HAS_PILLOW: return content, ext
    try:
        img = Image.open(io.BytesIO(content))
        img = img.convert("RGB")  # sang JPEG
        img.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=85)
        return output.getvalue(), ".jpg"
    except Exception as e:
        print(f"Lỗi resize cover: {e}")
        return content, ext

'''
<div style="float:left;margin:0 5px 0 0"> <script data-pagespeed-no-defer="" src="https://www.shizongzui.com/_,Mjo.wWu47RA2u8.js.pagespeed.jm.CeBdmWzuSy.js"></script><img src="data:image/webp;base64,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" width="157" height="219" alt="" data-pagespeed-url-hash="2827874089" onload="pagespeed.CriticalImages.checkImageForCriticality(this);"></div>

'''
def _extract_cover_url(soup: BeautifulSoup) -> str:
    # Tìm thẻ <div style="float:left;margin:0 5px 0 0"> chứa <img>
    div = soup.find("div", style=re.compile(r"float\s*:\s*left\s*;"))
    if div:
        img = div.find("img")
        if img and img.get("src"):
            return urljoin(Url, img["src"])
    return ""


def __save_epub(title: str, author: str, chapters: List[Dict[str, str]], cover_content: Optional[bytes], cover_ext: Optional[str]):
    '''
    tạo file EPUB từ thông tin truyện, danh sách chương, và cover đã tải về (nếu có).
    '''
    # Tạo thư mục tạm để lưu file trước khi nén
    temp_dir = f"temp_{_slugify(title)}"    
    os.makedirs(temp_dir, exist_ok=True)
    # Tạo file mimetype
    with open(os.path.join(temp_dir, "mimetype"), "w", encoding="utf-8") as f:
        f.write("application/epub+zip")
    # Tạo thư mục META-INF và file container.xml
    meta_inf_dir = os.path.join(temp_dir, "META-INF")
    os.makedirs(meta_inf_dir, exist_ok=True)
    with open(os.path.join(meta_inf_dir, "container.xml"), "w", encoding="utf-8") as f:
        f.write('''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>''')
    # Tạo file content.opf
    manifest_items = ""
    spine_items = ""
    for idx, chap in enumerate(chapters, 1):
        manifest_items += f'<item id="chap{idx}" href="chapter_{idx:03d}.html" media-type="application/xhtml+xml"/>\n'
        spine_items += f'<itemref idref="chap{idx}"/>\n'
    
    cover_item = ""
    if cover_content and cover_ext:
        cover_item = f'<item id="cover" href="cover{cover_ext}" media-type="image/{cover_ext[1:]}"/>\n'
        manifest_items += cover_item
    
    with open(os.path.join(temp_dir, "content.opf"), "w", encoding="utf-8") as f:
        f.write(f'''<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="uuid_id">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
        <dc:title>{title}</dc:title>
        <dc:creator>{author}</dc:creator>
        <dc:identifier id="uuid_id">urn:uuid:{uuid4()}</dc:identifier>
    </metadata>
    <manifest>
        {manifest_items}
    </manifest>
    <spine>
        {spine_items}
    </spine>
</package>''')
    # Lưu file chương    
    for idx, chap in enumerate(chapters, 1):
        with open(os.path.join(temp_dir, f"chapter_{idx:03d}.html"), "w", encoding="utf-8") as f:
            f.write(chap["content_html"])
    # Lưu file cover nếu có
    if cover_content and cover_ext:
        with open(os.path.join(temp_dir, f"cover{cover_ext}"), "wb") as f:
            f.write(cover_content)
    # Tạo file EPUB (zip)
    epub_filename = f"{_slugify(title)}.epub"
    with zipfile.ZipFile(epub_filename, "w", zipfile.ZIP_DEFLATED) as epub:
        epub.write(os.path.join(temp_dir, "mimetype"), "mimetype", compress_type=zipfile.ZIP_STORED) # mimetype phải là STORED
        for foldername, subfolders, filenames in os.walk(temp_dir):
            for filename in filenames:
                if filename == "mimetype":
                    continue
                file_path = os.path.join(foldername, filename)
                epub_path = os.path.relpath(file_path, temp_dir)
                epub.write(file_path, epub_path)
    # Xóa thư mục tạm
    shutil.rmtree(temp_dir)
    
    


# list truyện trong trang
#https://www.shizongzui.com/
#https://www.shizongzui.com/qianzhuan/
#https://www.shizongzui.com/diyibu/
#https://www.shizongzui.com/dierbu/
#https://www.shizongzui.com/disanbu/
#https://www.shizongzui.com/disibu/
#https://www.shizongzui.com/diwubu/
#https://www.shizongzui.com/diliubu/


Url = "https://www.shizongzui.com/"
infot = _get_book_info(_fetch_html(Url))

print("-----------------Thông tin truyện-----------------------------")
print("Title:", infot["title"])
print("Author:", infot["author"])

print("\n-----------------Danh sách chương-----------------------------")
chapters = _get_chapter_list(_fetch_html(Url))
print(f"Tổng số chương: {len(chapters)}")

print("\n-----------------Đang trích xuất và lưu nội dung chương-----------------------------")
# Dùng _safe_filename để đảm bảo thư mục tạo ra khớp 100% với đường dẫn lưu file
safe_title = _safe_filename(infot["title"])
output_dir = os.path.join("output", safe_title)
os.makedirs(output_dir, exist_ok=True)

all_chapters_content = []

# Duyệt và lưu NGAY LẬP TỨC từng chương
for idx, (chap_title, chap_url) in enumerate(chapters, 1):
    print(f"Đang xử lý chương {idx}/{len(chapters)}: {chap_title}")
    try:
        chap_soup = _fetch_html(chap_url)
        chap_content = _get_chapter_content(chap_soup, url=chap_url)
        
        # Lưu file HTML ra đĩa ngay sau khi parse xong
        filename = os.path.join(output_dir, f"chapter_{idx:03d}.html")
        _save_file_content(filename, chap_content)
        print(f"  -> Đã lưu: {filename}")
        
        # Đưa vào biến nhớ để lát nữa build EPUB
        all_chapters_content.append({
            "title": chap_title,
            "content_html": chap_content
        })
        time.sleep(SLEEP_BETWEEN_CHAPS)
    except Exception as e:
        print(f"⚠ Lỗi xử lý chương {chap_title} ({chap_url}): {e}")
        continue
        
print("\n-----------------Đang tải cover-----------------------------")
cover_url = _extract_cover_url(_fetch_html(Url))
cover_content, cover_ext = _download_cover(cover_url)
if cover_content and cover_ext:
    cover_content, cover_ext = _resize_cover(cover_content, cover_ext)
    cover_filename = os.path.join(output_dir, f"cover{cover_ext}")
    with open(cover_filename, "wb") as f:
        f.write(cover_content)
    print(f"Đã tải và lưu cover: {cover_filename}")
    
print("\n-----------------Đang tạo file EPUB-----------------------------")
# Truyền safe_title thay vì infot["title"] gốc nếu bạn muốn an toàn tối đa
__save_epub(safe_title, infot["author"], all_chapters_content, cover_content, cover_ext)
print(f"Đã tạo file EPUB thành công!")




