import requests, re, time
from bs4 import BeautifulSoup

# =============== CẤU HÌNH ===============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.69shuba.com/"
}

# ⚠️ Cookie này bạn phải copy từ trình duyệt (DevTools → Network → Request Headers)
COOKIES = {
    "zh_choose": "s",
    "shuba": "4872-3647-19663-1974"   # thay bằng giá trị thật từ trình duyệt
}

TIMEOUT = 20
RETRY_STATUS = {429, 500, 502, 503, 504}

def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""

def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    for k in range(tries):
        r = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=TIMEOUT)
        
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff*(k+1))
            continue
        
        if 400 <= r.status_code < 500 and r.status_code != 429:
            r.raise_for_status()

        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")

def _get_info(soup: BeautifulSoup) -> dict[str, str]:
    info = {}

    # Lấy tiêu đề
    title = soup.find("div", class_="booknav2")
    info["title"] = _text(title.find("h1"))

    # Lấy tác giả
    author_tag = soup.find("p", string=re.compile("作者"))
    if author_tag and author_tag.find("a"):
        info["author"] = _text(author_tag.find("a"))
    else:
        info["author"] = ""

    return info

# ================== TEST ==================
Url = "https://www.m.69shuba.com/book/56945.htm"
soup = _fetch_html(Url)
print(_get_info(soup))
