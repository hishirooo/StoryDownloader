# -*- coding: utf-8 -*-
'''
    @Author: Hishio - Cá mụp cắn cáp
    @github: https://github.com/hishirooo
    @date: 20-11-2025
    @version: 1.3 - Final fix: Cleaned chapter titles to only show "Chương xxx" for TOC/filename/log.
    @site: https://phongphongtam2.com/
'''


from bs4 import BeautifulSoup, Tag, Comment
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, urlunparse, urljoin
import requests, ssl, urllib3, re, json, html, os, unicodedata, zipfile, io, datetime, shutil, time
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import importlib, re, sys, os, io, glob

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

    title_node =  _text(soup.find("div",class_ = "post-title")).split("–")
    title = title_node[0].strip()
    author = title_node[1].strip()
    list_info['title'] = title
    list_info['author'] = author

    
    from bs4 import BeautifulSoup

    items = soup.find_all('div', class_='post-content_item')
    list_info['genre']="N/A"
    # tìm thể loại
    for item in items:
        heading = item.find('div', class_='summary-heading')
        if heading and 'Thể loại' in heading.text:
            content = item.find('div', class_='summary-content')
            if content:
                list_info['genre'] = content.text.strip()
                break
    
    # cover
    image_node =  soup.find("div",class_ = "summary_image")
 
    if image_node:
        img_tag = image_node.find('img')
        if img_tag and img_tag.has_attr('src'):
            image_url = img_tag['src']           
            list_info['cover'] = image_url
            
    # tình trạng 
    for item in items:
        heading = item.find('div', class_='summary-heading')
        if heading and 'Trạng thái' in heading.text:
            content = item.find('div', class_='summary-content')
            if content:
                list_info['status'] = content.text.strip()
                break        
            
            
    return list_info
        
        
def _get_list_chapters(url: str) -> list:
    chapters = []
    import requests

    url_post = url + "ajax/chapters"
    querystring = {"t": "1"}
    payload = ""

    headers = {
        "accept": "*/*",
        "accept-language": "vi,en-US;q=0.9,en;q=0.8",
        "content-length": "0",
        "origin": "https://phongphongtam2.com",
        "priority": "u=1, i",
        "referer": url,
        "sec-ch-ua": "\"Chromium\";v=\"142\", \"Microsoft Edge\";v=\"142\", \"Not_A Brand\";v=\"99\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
        "x-requested-with": "XMLHttpRequest"
    }

    response = requests.post(url_post, data=payload, headers=headers, params=querystring)

    if response.status_code == 200:
        soup = BeautifulSoup(response.text, "html.parser")
        ul_node = soup.find("ul",class_ = "main version-chap no-volumn")
        if ul_node:
            a_node = ul_node.find_all("a")
            for a in a_node:
                link = a["href"]
                title = _text(a)
                chapters.append({"title": title, "link": link})      
    return chapters       
        
        
def get_content_chapter(url: str) -> str:
    soup = _fetch_html(url)
    # body > div.wrap > div > div.site-content > div > div > div > div > div.main-col.col-md-8.col-sm-12.sidebar-hidden > div > div > div.entry-content > div > div > div > div.text-left
    
    content_node = soup.find("div",class_ = "entry-content")
    #ghi ra file html
    with open("content.html", "w", encoding="utf-8") as f:
        f.write(str(content_node))
    if content_node:

        # Xóa p rỗng, credit
        for p in content_node.find_all('p'):
            if 'Editor:' in p.text or 'Beta:' in p.text or p.text.strip() == '—' or not p.text.strip():
                p.decompose()

        # Xóa cảnh báo
        warning_div = content_node.find('div', class_='chapter-warning alert alert-warning')
        if warning_div:
            warning_div.decompose()

        # Chỉ giữ lại <p>, unwrap các thẻ khác
        for tag in content_node.find_all(recursive=True):
            if isinstance(tag, Comment):
                continue
            if tag.name != "p":
                tag.unwrap()

        # Xóa comment "CONTENT END"
        for comment in content_node.find_all(string=lambda text: isinstance(text, Comment)):
            if "CONTENT END" in comment:
                comment.extract()
        
        # chỉ giữ lại <p>, unwrap các thẻ khác
        content_node = content_node.find_all("p")
        # join lại các <p> với \n vẫn giữ <p> </p>
        content_node = "\n".join([str(p) for p in content_node])
        

        with open("content.html", "w", encoding="utf-8") as f:
            f.write(str(content_node))
        
        return content_node
        
    else:
        print("Không tìm thấy nội dung")
    
    
        
UrlStory= "https://phongphongtam2.com/manga/tinh-yeu-den-muon-diep-kien-tinh/"
info = _get_book_info(_fetch_html(UrlStory))
print("-----------------------------INFO TRUYỆN-----------------------------")
print(f"Title : {info.get('title')}")
print(f"Author: {info.get('author')}")
print(f"Genre : {info.get('genre')}")
print(f"Status : {info.get('status')}")
print(f"Cover URL : {info.get('cover')}")


chapters = _get_list_chapters(UrlStory)
print(f"Total chapters: {len(chapters)}")


get_content_chapter(chapters[2]["link"])

