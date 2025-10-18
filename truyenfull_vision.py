# -*- coding: utf-8 -*-
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, urlunparse, urljoin
import requests, ssl, urllib3, re, json, html, os, unicodedata, zipfile, io, datetime, shutil, time
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
# =============== CẤU HÌNH ===============
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}


def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""

def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    for k in range(tries):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff*(k+1)); continue
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")
#----------------------INFO TRUYỆN----------------------#
# Lấy thông tin truyện return Dict với keys: title, author, genre, status
def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    """Lấy title/author/genre/status theo layout  (có fallback)."""
    info = {"title": "", "author": "", "genre": "", "status": ""}
    try:
        # Title
        title = _text(soup.find("h3",class_="title"))
        if not info["title"] and soup.title:
            info["title"] = _text(soup.title)
        # Author    
        author = None
        n = soup.select_one('a[itemprop="author"]')
        if n:
            author = _text(n)
        else:
            h3 = soup.select_one('h3:-soup-contains("Tác giả")')
            if h3:
                a = h3.find_next("a")
                if a: author = _text(a)
        # Genres
        genres = [_text(a) for a in soup.select('a[itemprop="genre"]') if _text(a)]
        if not genres:
            h3 = soup.select_one('h3:-soup-contains("Thể loại")')
            if h3:
                container = h3.find_parent() or h3.parent
                if container:
                    for a in container.find_all("a"):
                        t = _text(a)
                        if t: genres.append(t)
        seen=set(); genres=[g for g in genres if not (g in seen or seen.add(g))]
        # Status <span class="text-primary">Đang ra</span>

        status = soup.select_one("div.info span.text-primary")
        if status: 
            status = _text(status)
        else: status = "N/A"

        info["Title :"] = title
        info["Author :"] = author
        info["Status :"] = status
        info["Genres :"]  = genres
    except Exception:
        pass
    return info    
# Lấy ảnh bìa truyện
def _fetch_cover_from_book_page(book_page_url: str):
    '''Lấy ảnh bìa từ trang truyện. Trả về (nội dung ảnh bytes, đuôi mở rộng, url ảnh) hoặc (None, None, None).'''
    soup = _fetch_html(book_page_url)
    img = (soup.select_one(".book img[itemprop='image']") or
           soup.select_one(".books img[itemprop='image']") or
           soup.select_one(".book img") or
           soup.select_one(".books img") or
           soup.select_one("img[itemprop='image']"))
    if img and img.get("src"):
        src = urljoin(book_page_url, img["src"])
        r = requests.get(src, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        content = r.content
        ct = r.headers.get("Content-Type","").lower()
        ext = ".png" if ("png" in ct or src.lower().endswith(".png")) else ".jpg"
        return content, ext, src
    return None, None, None
# Lấy số trang mục lục
def _get_total_pages(soup: BeautifulSoup) -> int:
    '''Lấy tổng số trang mục lục từ soup trang 1.'''
    inp = soup.select_one('input#total-page[value]')
    if inp and inp["value"].isdigit():
        return int(inp["value"])
    last_a = soup.select_one('#list-chapter ul.pagination.pagination-sm a:-soup-contains("Cuối")')
    if last_a and last_a.get("href"):
        m = re.search(r"/trang-(\d+)/", last_a["href"])
        if m: return int(m.group(1))
    nums = []
    for a in soup.select('#list-chapter ul.pagination.pagination-sm a[href]'):
        m = re.search(r"/trang-(\d+)/", a["href"])
        if m: nums.append(int(m.group(1)))
    return max(nums) if nums else 1

def _build_page_url(page1_url: str, soup_page1: BeautifulSoup, page_idx: int) -> str:
    '''
    Xây URL trang mục lục thứ page_idx dựa vào URL trang 1 và soup trang 1.
    '''
    for a in soup_page1.select('#list-chapter ul.pagination.pagination-sm a[href]'):
        href = a["href"]
        if "/trang-" in href:
            replaced = re.sub(r"/trang-\d+/?", f"/trang-{page_idx}/", href)
            return replaced if replaced.startswith("http") else urljoin(page1_url, replaced)
    base = urlparse(page1_url)._replace(fragment="").geturl().rstrip("/")
    return f"{base}/trang-{page_idx}/#list-chapter"

def normalize_chapter_title(t: Optional[str]) -> Optional[str]:
    """Sửa 'Chương10:...' -> 'Chương 10: ...'."""
    if not t: return t
    t = t.strip()
    m = re.match(r"^(Chương)\s*(\d+)(\s*[:\-–]?\s*)(.*)$", t, flags=re.I)
    if m:
        name, num, _, rest = m.groups()
        rest = rest.strip()
        return f"{name} {num}: {rest}" if rest else f"{name} {num}"
    return t

def _extract_chapters_on_page(soup: BeautifulSoup, base_url: str):
    '''
    Lấy danh sách chương trên 1 trang mục lục.
    Trả về List[Dict] với keys: title, url
    '''
    chs = []
    for a in soup.select('#list-chapter ul.list-chapter a[href]'):
        title = normalize_chapter_title(a.get_text(strip=True))
        href  = urljoin(base_url, a["href"])
        if title and href:
            chs.append({"title": title, "url": href})
    return chs

def _clean_to_list_url(url: str) -> str:
    '''
    Chuyển URL trang truyện sang URL trang mục lục.
    Ví dụ:
    https://truyenfull.net/truyen/xxx -> https://truyenfull.net/truyen/xxx#list-chapter
    ''' 
    p = urlparse(url)
    p = p._replace(query="", fragment="list-chapter")
    return urlunparse(p)

def _get_list_chapters(book_page_url: str) -> List[Dict[str, str]]:
    '''
    Lấy danh sách chương từ trang truyện.
    Trả về List[Dict] với keys: title, url
    '''
    list_url = _clean_to_list_url(book_page_url)
    soup_page1 = _fetch_html(list_url)
    total_pages = _get_total_pages(soup_page1)
    all_chapters, seen = [], set()
    for c in _extract_chapters_on_page(soup_page1, list_url):
        if c["url"] not in seen:
            seen.add(c["url"])
            all_chapters.append(c)
    for i in range(2, total_pages + 1):
        page_url = _build_page_url(list_url, soup_page1, i)
        soup_i   = _fetch_html(page_url)
        for c in _extract_chapters_on_page(soup_i, page_url):
            if c["url"] not in seen:
                seen.add(c["url"])
                all_chapters.append(c)
        time.sleep(SLEEP_BETWEEN_PAGES)
    return all_chapters



def main():
    url = "https://truyenfull.vision/ban-trai-toi-khong-phai-la-nguoi-nhat-tiet-ngau/"
    chapter_list = _get_list_chapters(url)
    print("list=", len(chapter_list), chapter_list[:len(chapter_list)])


if __name__ == "__main__":
    main()
