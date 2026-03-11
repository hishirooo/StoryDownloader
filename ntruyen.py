"""ntruyen.py

Helper lấy novelId + danh sách chương từ ntruyen.biz.

Link chương có format:
    https://ntruyen.biz/doc-truyen/{book_slug}-{chapter_slug}-{chapter_id}

Ví dụ:
    doc_base_url = https://ntruyen.biz/doc-truyen/canh-cua-trong-khe-nut-matthia
    chapter_slug = chuong-1
    chapter_id   = 4521179
 -> https://ntruyen.biz/doc-truyen/canh-cua-trong-khe-nut-matthia-chuong-1-4521179

Ghi chú:
- Một số máy có thể bị 403 khi requests vào trang web ntruyen.biz (/truyen/...).
  Khi đó, bạn vẫn có thể lấy novelId bằng cách “view-source” và search "novelId",
  rồi gọi API trực tiếp: https://api.ntruyen.biz/novels/{novelId}/chapters
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


NTRUYEN_WEB = "https://ntruyen.biz"
NTRUYEN_API = "https://api.ntruyen.biz"

# Headers cho WEB (HTML)
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    "Referer": f"{NTRUYEN_WEB}/",
    "Connection": "keep-alive",
}

# Headers cho API (JSON)
API_HEADERS = {
    "User-Agent": WEB_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Origin": NTRUYEN_WEB,
    "Referer": f"{NTRUYEN_WEB}/",
}


_web = requests.Session()
_web.headers.update(WEB_HEADERS)

_api = requests.Session()
_api.headers.update(API_HEADERS)


@dataclass
class BookInfo:
    title: str = ""
    author: str = ""
    status: str = ""
    cover_url: str = ""


# --------------------------- Utils ---------------------------

def extract_novel_id(text: str) -> int:
    """Bắt novelId từ chuỗi (cả dạng thường và dạng bị escape).

    Bắt được cả:
        "novelId":39390
        {"novelId":39390}
        {\"novelId\":39390}
    """
    m = re.search(r'\\?"novelId\\?"\s*:\s*(\d+)', text)
    if not m:
        raise ValueError("Không tìm thấy novelId trong chuỗi")
    return int(m.group(1))


def _get_text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""


def get_slug_from_truyen_url(url: str) -> str:
    """/truyen/<slug> -> slug"""
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "truyen":
        return parts[1]
    raise ValueError("URL không đúng dạng https://ntruyen.biz/truyen/<slug>")


def get_slug_from_doc_url(url: str) -> str:
    """/doc-truyen/<slug> -> slug"""
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "doc-truyen":
        return parts[1]
    raise ValueError("URL không đúng dạng https://ntruyen.biz/doc-truyen/<slug>")


def to_doc_base_url(any_book_url: str) -> str:
    """Nhận URL /truyen/<slug> hoặc /doc-truyen/<slug> -> trả về doc base URL."""
    u = urlparse(any_book_url)
    parts = u.path.strip("/").split("/")

    if len(parts) >= 2 and parts[0] == "doc-truyen":
        book_slug = parts[1]
    elif len(parts) >= 2 and parts[0] == "truyen":
        book_slug = parts[1]
    else:
        raise ValueError("URL phải là /truyen/<slug> hoặc /doc-truyen/<slug>")

    return f"{NTRUYEN_WEB}/doc-truyen/{book_slug}"


def build_chapter_url(doc_base_url: str, chapter_slug: str, chapter_id: int) -> str:
    """doc_base_url + -{chapter_slug}-{chapter_id}"""
    book_slug = get_slug_from_doc_url(doc_base_url)
    return f"{NTRUYEN_WEB}/doc-truyen/{book_slug}-{chapter_slug}-{int(chapter_id)}"


# --------------------------- WEB (HTML) ---------------------------

def fetch_html(url: str, timeout: int = 30) -> BeautifulSoup:
    """Tải HTML bằng session.

    Nếu bị 403, thử warm-up (hit homepage) rồi retry 1 lần.
    """
    r = _web.get(url, timeout=timeout)

    if r.status_code == 403:
        # warm-up cookie (nếu site set)
        try:
            _web.get(f"{NTRUYEN_WEB}/", timeout=timeout)
        except Exception:
            pass
        r = _web.get(url, timeout=timeout)

    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding

    return BeautifulSoup(r.text, "html.parser")


def get_novel_id_from_soup(soup: BeautifulSoup) -> int:
    return extract_novel_id(str(soup))


def parse_book_info(soup: BeautifulSoup) -> BookInfo:
    """Parse thông tin cơ bản.

    Lưu ý: selector có thể thay đổi theo giao diện site.
    """
    title = _get_text(soup.find("h1", itemprop="name"))

    author = _get_text(
        soup.find(
            "div",
            class_="text-sm text-black/65 dark:text-white/65 flex flex-wrap items-center gap-1",
        )
    ).replace("Tác giả:", "").strip()

    status = _get_text(soup.find("span", itemprop="bookFormat")).strip()

    cover_url = ""
    cover_box = soup.find(
        "div",
        class_="relative flex-shrink-0 w-[179px] h-[234px] sm:w-[219px] sm:h-[288px]",
    )
    if cover_box and cover_box.find("img") and cover_box.find("img").get("src"):
        cover_url = cover_box.find("img")["src"].split("?")[0]

    return BookInfo(title=title, author=author, status=status, cover_url=cover_url)


# --------------------------- API (JSON) ---------------------------

def fetch_chapters_page(
    novel_id: int,
    page: int = 1,
    limit: int = 50,
    sort: str = "asc",
    keyword: str = "",
    timeout: int = 30,
) -> Dict:
    """GET /novels/{id}/chapters"""
    url = f"{NTRUYEN_API}/novels/{int(novel_id)}/chapters"
    params = {"page": int(page), "keyword": keyword, "limit": int(limit), "sort": sort}
    r = _api.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_all_chapters(
    novel_id: int,
    limit: int = 50,
    sort: str = "asc",
    keyword: str = "",
) -> List[Dict]:
    """Lấy toàn bộ chapters bằng API (dựa vào totalPages trong response)."""
    first = fetch_chapters_page(novel_id, page=1, limit=limit, sort=sort, keyword=keyword)
    total_pages = int(first.get("totalPages", 1))

    chapters: List[Dict] = list(first.get("chapters", []))
    for p in range(2, total_pages + 1):
        j = fetch_chapters_page(novel_id, page=p, limit=limit, sort=sort, keyword=keyword)
        chapters.extend(j.get("chapters", []))

    return chapters


def attach_chapter_urls(doc_base_url: str, chapters: List[Dict]) -> List[Dict]:
    """Gắn field url vào từng chapter theo format ntruyen."""
    out: List[Dict] = []
    for ch in chapters:
        cid = ch.get("id")
        cslug = ch.get("slug")
        if cid is None or not cslug:
            continue
        item = dict(ch)
        item["url"] = build_chapter_url(doc_base_url, str(cslug), int(cid))
        out.append(item)
    return out


# --------------------------- High-level helpers ---------------------------

def get_all_chapters_for_book_url(
    book_url: str,
    limit: int = 50,
    sort: str = "asc",
    keyword: str = "",
    debug_save_html: Optional[str] = None,
) -> Dict:
    """Nhập URL /truyen/<slug> hoặc /doc-truyen/<slug> -> trả dict:

    {
        "novelId": int,
        "docBaseUrl": str,
        "info": BookInfo,
        "chapters": [ {id,name,slug,url,...}, ... ]
    }

    Nếu bị 403 khi tải HTML, bạn có thể lấy novelId thủ công và dùng get_all_chapters_by_id().
    """
    doc_base_url = to_doc_base_url(book_url)

    # Luôn cố tải từ /truyen/<slug> vì trang đó thường có __NEXT_DATA__/novelId
    if "/truyen/" in book_url:
        book_page_url = book_url
    else:
        # nếu người dùng đưa /doc-truyen/<slug>, thử map về /truyen/<slug>
        book_slug = get_slug_from_doc_url(book_url)
        book_page_url = f"{NTRUYEN_WEB}/truyen/{book_slug}"

    soup = fetch_html(book_page_url)

    if debug_save_html:
        with open(debug_save_html, "w", encoding="utf-8") as f:
            f.write(str(soup))

    novel_id = get_novel_id_from_soup(soup)
    info = parse_book_info(soup)

    chapters = fetch_all_chapters(novel_id, limit=limit, sort=sort, keyword=keyword)
    chapters = attach_chapter_urls(doc_base_url, chapters)

    return {
        "novelId": novel_id,
        "docBaseUrl": doc_base_url,
        "info": info,
        "chapters": chapters,
    }


def get_all_chapters_by_id(
    novel_id: int,
    doc_base_url: str,
    limit: int = 50,
    sort: str = "asc",
    keyword: str = "",
) -> Dict:
    """Dùng khi bạn đã có novel_id, không cần tải HTML (né 403)."""
    chapters = fetch_all_chapters(novel_id, limit=limit, sort=sort, keyword=keyword)
    chapters = attach_chapter_urls(doc_base_url, chapters)
    return {
        "novelId": int(novel_id),
        "docBaseUrl": doc_base_url,
        "chapters": chapters,
    }

def _get_content_from_chapter_url(chapter_url: str, timeout: int = 3) -> str:
    """Lấy nội dung chương từ URL chương."""
    soup = fetch_html(chapter_url, timeout=timeout)
    
    # ghi ra file để debug
    with open("debug_chapter.html", "w", encoding="utf-8") as f:
        f.write(str(soup))
    content_div = soup.find("div", class_="reading-content")
    if not content_div:
        raise ValueError("Không tìm thấy nội dung chương trong trang.")
    return str(content_div)



# --------------------------- Demo ---------------------------

if __name__ == "__main__":
    # Ví dụ:
    # book_url = "https://ntruyen.biz/truyen/canh-cua-trong-khe-nut-matthia"
    book_url = "https://ntruyen.biz/truyen/quan-tai-mo-tram-ma-tan-vuong-phi-tu-dia-nguc-tro-ve"

    print("---------------Info-----------------")
    try:
        data = get_all_chapters_for_book_url(book_url, limit=50, debug_save_html="debug_book.html")
        print("novelId =", data["novelId"])
        print("docBaseUrl =", data["docBaseUrl"])
        print("title =", data["info"].title)
        print("chapters =", len(data["chapters"]))
        
        print("---------------Get content-------------------")
        _get_content_from_chapter_url(data["chapters"][0]["url"]) 
    except requests.HTTPError as e:
        print("HTTPError:", e)
        print("Nếu bị 403 khi tải HTML, hãy lấy novelId thủ công (view-source) rồi dùng get_all_chapters_by_id().")

    # Ví dụ dùng novelId thủ công:
    # doc_base = "https://ntruyen.biz/doc-truyen/canh-cua-trong-khe-nut-matthia"
    # data2 = get_all_chapters_by_id(39390, doc_base)
    # print("chapters =", len(data2["chapters"]))
