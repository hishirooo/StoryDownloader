# -*- coding: utf-8 -*-
'''
    @Author: Hishio - Cá mụp cắn cáp
    @github: https://github.com/hishirooo
    @date: 18-10-2025
    @version: 1.0
    truyenfull_vision.py - Tải truyện và tạo EPUB (truyenfull.vision)
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

def _get_info(soup: BeautifulSoup) -> Dict[str, str]:
    '''
    Lấy thông tin truyện từ thẻ BeautifulSoup.
    '''
    title_node = soup.find("h1", class_="entry-title")
    # <h1 class="entry-title">[Trinh Thám] Năm Ấy Anh Từng Đến | Vân Khởi Phong Miên</h1>
    # [Trinh Thám] Năm Ấy Anh Từng Đến | Vân Khởi Phong
    # Tách [Thể loại] ra khỏi tiêu đề lưu vào genre nếu có
    # Phần còn lại là Tên truyện | Tác giả
    info = re.search(r"\[(.+)\] (.+)\|(.+)", _text(title_node))
    if info:
        genre = info.group(1).strip()
        title = info.group(2).strip()
        author = info.group(3).strip()

    status_node = soup.find("span", class_="cat-links")
    if status_node:
        status = _text(status_node)

    cover_node = soup.find("div", class_="wp-block-image").find('img', {'data-orig-file': True})
    if cover_node:
        cover_url = cover_node['data-orig-file']

    print(f'title: {title}')
    print(f'author: {author}')
    print(f'genre: {genre}')
    print(f'status: {status}')
    print(f'cover: {cover_url}')
    return {
        "title": title,
        "author": author,
        "genre": genre,
        "status": status,
        "cover": cover_url
    }


def _get_list_chapters(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    '''
    Lấy danh sách chương từ thẻ BeautifulSoup.
    Trả về danh sách (chapter_title, chapter_url).
    '''
    chapters = []


url = "https://shiningjustforme.wordpress.com/2021/08/11/truyen-dich-nam-ay-anh-tung-den-van-khoi-phong/"
_get_info(_fetch_html(url))