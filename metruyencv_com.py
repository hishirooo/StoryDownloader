'''
    @Author: Hishio - Cá mụp cắn cáp
    @github: https://github.com/hishirooo
    @date: 18-10-2025
    @version: 1.0
    get truyện từ metruyencv.com
    https://metruyencv.com/
    Chế độ :
        1. Tài HTML
        2. Tài TXT
        3. Tài HTML + Tạo EPUB (đọc lại HTML có sẵn)
        4. Tài TXT  + Tạo EPUB (TXT -> bọc XHTML tối giản -> EPUB)
        5. Tài HTML + TXT + Tạo EPUB (EPUB dùng HTML có sẵn)
    Cover:
        - Sau khi nhập URL, nhập đường dẫn cover (file local hoặc URL ảnh).
        - Bỏ trONGL sẽ tự lý cover từ DOM: div.book-img img[src]
        - Nếu ảnh khóa hình → convert 1 lần sang JPEG (cần Pillow).
    Log:
        - Thư mục đầu ra: ./Output/<TieuDeTruyen> (không dấu hoặc có dấu tùy cấu hình)
        - EPUB sẽ lưu ở: ./Output/<TieuDeTruyen>.epub (không nằm trong thư mục truyện)
        - Khi tải:   Saved : 0001.xhtml - TieuDeChuong
        - Khi tạo:   Readfile : 0001.xhtml from Output/<TieuDeTruyen>
    
'''

from bs4 import BeautifulSoup
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

def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    '''
    Tải HTML (có chuyển domain + fallback verify=False khi SSLError).
    '''
    for k in range(tries):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        
        # Chỉ retry các lỗi server (5xx) và 429
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status() # Lần cuối mà vẫn lỗi thì raise
            time.sleep(backoff*(k+1)); continue
        
        # Nếu là lỗi client (4xx) khác 429, dừng retry và raise
        if 400 <= r.status_code < 500:
            r.raise_for_status() 

        r.raise_for_status() # Các status code 2xx thành công
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")
#----------------------INFO TRUYỆN----------------------#

def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    '''
    Lấy thông tin truyện từ trang chính.
    Trả về dict với các key:
        - title: Tiêu đề truyện
        - author: Tác giả
        - genres: Thể loại
        - status: Trạng thái
        - cover_url: URL ảnh bìa
    '''
    
    info = {}
    info_node = soup.find("div", class_="mb-4 mx-auto text-center md:mx-0 md:text-left")
    # Tiêu đề truyện
    info['title'] = _text(info_node.select_one('a.text-title'))

    # Tác giả
    author = _text(info_node.select_one('a.text-gray-500'))

    
    genres_node = soup.find("div", class_="leading-10 md:leading-normal space-x-4")
    # trạng thái
    status = genres_node.select("a")[0]
    info['status'] = _text(status)
    # thể loại
    genres = [ _text(a) for a in genres_node.select("a")[1:] ]
    info['genres'] = " - ".join(genres)
    # image cover
    image_node = soup.find("div", class_="mb-4 md:mb-0 md:mr-6").find("img",class_="w-44 h-60 shadow-lg rounded mx-auto").get("src")
    print(image_node)
    info['cover_url'] = image_node  
    return info

def _get_story_id(soup: BeautifulSoup) -> str:
    '''
    Lấy story id từ trang chính.
    '''
    story_id_node = soup.find("div", class_="block mx-auto mb-6 md:mb-0 md:inline-flex").get("data-x-data")
    story_id = re.search(r'readings\((\d+)\)', story_id_node).group(1)
    return story_id

def _get_chapter_list(soup: BeautifulSoup) -> List[Dict[str, str]]:
    ''' 
    Lấy danh sách chương bằng api
    '''
    chapter_list = []
    # Lấy story id
    story_id = _get_story_id(soup)
    # Gọi API để lấy danh sách chương https://backend.metruyencv.com/api/chapters?filter%5Bbook_id%5D=131847&filter%5Btype%5D=published
    url = f"https://backend.metruyencv.com/api/chapters?filter%5Bbook_id%5D={story_id}&filter%5Btype%5D=published"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    for chapter in data['data']:
        chap_info = {
            'name': chapter['attributes']['name'],
            'index': chapter['attributes']['index'],
        }
        chapter_list.append(chap_info)
    return chapter_list
    
def _gen_list_chapters(StoryUrl: str) -> List[Dict[str, str]]:
    '''
    tạo danh sách chương từ get_chapter_list 
    '''
    chapter_list = _get_chapter_list(_fetch_html(StoryUrl))
    chapters = []
    for chap in chapter_list:
        chap_url = f"{StoryUrl}/chuong-{chap['index']}"
        chapters.append({
            'title': chap['name'],
            'url': chap_url
        })
    return chapters   

UrlStory = "https://metruyencv.com/truyen/vot-thi-nhan"
_get_book_info(_fetch_html(UrlStory))
_get_story_id(_fetch_html(UrlStory))


