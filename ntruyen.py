
from typing import Optional, List, Dict, Tuple
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, urljoin
import requests, ssl, urllib3, re, json, html, os, unicodedata, zipfile, io, datetime, shutil

from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# ====================== CẤU HÌNH ======================

BASE = "https://truyen.tangthuvien.vn"     # mirror ổn định
OUTPUT_ROOT = os.path.join(os.getcwd(), "Output")
USE_NO_DIACRITICS_FOLDER = True            # True: tên thư mục không dấu; False: giữ dấu
SAVE_AS_XHTML = True                       # Lưu trang chương là .xhtml

# ---- EPUB target (mặc định EPUB 2 an toàn cho Kobo) ----
EPUB_TARGET = "epub2"   # "epub2" | "epub3"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

# ====================== SESSION + TLS ======================

class TLSAdapter(HTTPAdapter):
    """Adapter ép TLS >= 1.2 và retry hợp lý."""
    def __init__(self, **kwargs):
        self._ctx = ssl.create_default_context()
        try:
            self._ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except Exception:
            pass
        retries = Retry(
            total=3, connect=3, read=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            raise_on_status=False,
        )
        super().__init__(max_retries=retries, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx
        return super().init_poolmanager(*args, **kwargs)

_session = requests.Session()
_session.headers.update(HEADERS)
_session.mount("https://", TLSAdapter())
_session.mount("http://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5)))

# ====================== TIỆN ÍCH ======================

def _canonicalize_domain(url: str) -> str:
    """Chuyển *.tangthuvien.net -> truyen.tangthuvien.vn (tránh lỗi TLS)."""
    u = urlparse(url)
    host = (u.netloc or "").lower()
    if host.endswith("tangthuvien.net"):
        u = u._replace(scheme="https", netloc="truyen.tangthuvien.vn")
        return urlunparse(u)
    return url

def _fetch_html(url: str) -> BeautifulSoup:
    """Tải HTML (có chuyển domain + fallback verify=False khi SSLError)."""
    url = _canonicalize_domain(url)
    try:
        r = _session.get(url, timeout=30)
        r.raise_for_status()
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = _session.get(url, timeout=30, verify=False)
        r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "html.parser")

def _download_bytes(url: str):
    """Tải bytes (ví dụ ảnh cover)."""
    url = _canonicalize_domain(url)
    try:
        resp = _session.get(url, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = _session.get(url, timeout=30, verify=False)
        resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type")

def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""

def slugify_filename(name: str, allow_unicode: bool = False) -> str:
    """Chuẩn hoá tên thư mục/tệp hợp lệ, bỏ gạch '-' theo yêu cầu."""
    name = (name or "").strip().replace("—","-").replace("–","-")
    name = name.replace("-", "").strip()  # bỏ gạch ngang
    if allow_unicode:
        name = unicodedata.normalize("NFKC", name)
    else:
        name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\-. ]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    invalid = {"CON","PRN","AUX","NUL","COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8","COM9",
               "LPT1","LPT2","LPT3","LPT4","LPT5","LPT6","LPT7","LPT8","LPT9"}
    if name.upper() in invalid:
        name = "_" + name
    return name or "output"

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def _get_info(soup: BeautifulSoup):
    '''get info from soup of book page'''
    
    # ghi ra file HTML để debug
    with open("debug_book.html", "w", encoding="utf-8") as f:
        f.write(str(soup))
    # <h1 class="font-semibold text-xl text-center sm:text-left" itemprop="name">Cánh Cửa Trong Khe Nứt</h1>    
    title = _text(soup.find("h1", itemprop="name"))
    print("Title:", title)
    
    author = _text(soup.find("div", class_="text-sm text-black/65 dark:text-white/65 flex flex-wrap items-center gap-1")).replace("Tác giả: ", "")
    print("Author:", author)
    #<span class="text-sm">Trạng thái: <span itemprop="bookFormat">Đã hoàn thành</span>
    status = _text(soup.find("span", itemprop="bookFormat"))
    print("Status:", status)
    
    genres=[]
    # cover = soup.find("img", class_="cover")["src"] if soup.find("img", class_="cover") else ""
    # return {
    #     "title": title,
    #     "author": author,
    #     "cover": cover,
    # }


def _get_content_chapter(soup: BeautifulSoup):
    """Lấy nội dung chương từ BeautifulSoup của trang chương."""
    content = soup
    # ghi ra file XHTML/HTML để debug
    filename = "debug_chapter.xhtml" if SAVE_AS_XHTML else "debug_chapter.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(str(content))

url ="https://ntruyen.biz/truyen/canh-cua-trong-khe-nut-matthia"

_get_info(_fetch_html(url))
#_get_content_chapter(_fetch_html(url))