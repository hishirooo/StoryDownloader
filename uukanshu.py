from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, urlunparse, urljoin
import ssl, urllib3, re, json, html, os, unicodedata, zipfile, io, datetime, shutil, time
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
try:
    from curl_cffi import requests
except ImportError:
    os.system("pip install curl_cffi")
    import requests
import importlib, sys, glob

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
        # Dùng impersonate='chrome110' để bypass 403
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, impersonate="chrome110")
        
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
#----------------------INFO TRUYỆN----------------------#


def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:

    info = {
        "title": "Unknown",
        "genre": "N/A",
        "cover_url": "",
        "intro": "",
    }

    h1 = soup.find("h1", class_="booktitle")
    if h1: info["title"] = _text(h1)

    genre = soup.find("span",class_="blue").find_next("span") # thẻ span thứ 2
    if genre: info["genre"] = _text(genre)

    intro = soup.find("p",class_="bookintro")
    if intro: info["intro"] = _text(intro)

    img_cover = soup.find("img",class_="thumbnail")
    if img_cover: info["cover_url"] = img_cover["src"]  

    return info
    
def _get_list_chapters(soup: BeautifulSoup) -> List[Dict[str, str]]:
    chap_list = []
    div_list = soup.find("div",id="list-chapterAll")
    if not div_list: return []
    for li in div_list.find_all("dd"):
        a = li.find("a", href=True)
        if a: chap_list.append({"title": _text(a), "url": "https://uukanshu.cc" + a["href"]})

    return chap_list

def _get_chapter_content(soup: BeautifulSoup) -> str:
    """Lấy nội dung chương truyện."""
    content = soup.find("div", class_="readcotent bbb font-normal")
    if content: return _text(content)
    return ""




url = "https://uukanshu.cc/book/10500/"

info = _get_book_info(_fetch_html(url))
print("------------Info------------------")
print("Title: ",info["title"])
print("Genre: ",info["genre"])
print("Cover URL: ",info["cover_url"])
print("Intro: ",info["intro"])

list_chapter = _get_list_chapters(_fetch_html(url))
print("Total chapter:",len(list_chapter))

# testchapter = _get_chapter_content(_fetch_html(list_chapter[100]["url"]))
# print("------------Test Chapter------------------")
# print(list_chapter[100]["url"])
# print(testchapter)
