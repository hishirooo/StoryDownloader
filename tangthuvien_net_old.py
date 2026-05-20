# -*- coding: utf-8 -*-
"""
TangThuVien extractor (stabilized)
- Ưu tiên TOC "một phát" qua /story/chapters?story_id=...
- Fallback: HTML + AJAX phân trang với vòng hội tụ, retry/backoff, domain fallback (.vn <-> .net)
- Dedupe theo URL, sort theo chap_no rồi title
- Trích nội dung chương từ .box-chap (ưu tiên)

Drop-in thay cho tangthuvien_net.py cũ của bạn.
"""
from __future__ import annotations

import os, re, html, unicodedata, ssl, time, random
from typing import List, Dict, Optional, Tuple, Set
from download_logger import chapter_log_line

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, urljoin

# ===================== CONFIG =====================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi,vi-VN;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}
TIMEOUT_HTML = 30
TIMEOUT_AJAX = 20
INSECURE_SSL = False  # nếu cần bỏ kiểm chứng SSL (không khuyến nghị)

# mirrors của site
MIRRORS = ["tangthuvien.net", "truyen.tangthuvien.vn"]  # .net ưu tiên trước vì ổn định hơn

# AJAX crawl fallback
AJAX_LIMIT = 75
AJAX_BACKOFF_BASE = 0.6
AJAX_JITTER = (0.25, 0.9)  # sleep ngẫu nhiên giữa các trang

# ===================== TLS Adapter =====================
class TLS12HttpAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except Exception:
            ctx.options |= ssl.OP_NO_TLSv1
            ctx.options |= ssl.OP_NO_TLSv1_1
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        ctx = create_urllib3_context()
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except Exception:
            ctx.options |= ssl.OP_NO_TLSv1
            ctx.options |= ssl.OP_NO_TLSv1_1
        kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(*args, **kwargs)


def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = TLS12HttpAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s

# ===================== UTIL =====================

def _clean_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s, flags=re.S).strip()
    return s


def slugify_vi(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or "truyen"


def _with_domain(url: str, new_domain: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme or "https", new_domain, p.path or "/", p.params, p.query, p.fragment))


def _fetch_html(url: str) -> BeautifulSoup:
    sess = _make_session()
    first_host = urlparse(url).netloc.lower()
    candidate_domains = [first_host] if first_host else []
    for m in MIRRORS:
        if m not in candidate_domains:
            candidate_domains.append(m)

    last_err = None
    for host in candidate_domains:
        try:
            test_url = _with_domain(url, host)
            resp = sess.get(test_url, timeout=TIMEOUT_HTML, verify=not INSECURE_SSL)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.exceptions.RequestException as e:
            last_err = e
            continue

    if not INSECURE_SSL and last_err:
        try:
            resp = sess.get(url, timeout=TIMEOUT_HTML, verify=False)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception:
            raise last_err
    if last_err:
        raise last_err
    raise RuntimeError("Không tải được trang")


def _pick_title(soup: BeautifulSoup) -> Optional[str]:
    for sel in ["h1", "h2", "h3", ".title h1", ".book-title h1", ".book-title", ".title"]:
        n = soup.select_one(sel)
        if n and _clean_text(n.get_text()):
            return _clean_text(n.get_text())
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        return _clean_text(og["content"])
    t = soup.find("title")
    if t and _clean_text(t.get_text()):
        return _clean_text(t.get_text())
    return None

# ===================== META HELPERS =====================
LABEL_KEYS = {
    "author": ["tác giả", "tac gia", "tác-giả", "author"],
    "genres": ["thể loại", "the loai", "thể-loại", "genre", "thể loại(s)"],
    "status": ["tình trạng", "trạng thái", "tinh trang", "trang thai", "status"],
}


def _gather_label_value_pairs(soup: BeautifulSoup) -> List[Dict[str, str]]:
    pairs = []
    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt"); dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label = _clean_text(dt.get_text())
            value = _clean_text(dd.get_text(" ", strip=True))
            if label and value:
                pairs.append({"label": label, "value": value})
    for tr in soup.find_all("tr"):
        th = tr.find("th") or tr.find("td")
        tds = tr.find_all("td")
        if th and len(tds) >= 1:
            label = _clean_text(th.get_text())
            value_nodes = tds[1:] if len(tds) > 1 else tds
            value = _clean_text(" ".join(v.get_text(" ", strip=True) for v in value_nodes))
            if label and value:
                pairs.append({"label": label, "value": value})
    for li in soup.find_all("li"):
        key_node = None
        for sel in ["span", "b", "strong", "h3", "label", "em"]:
            cand = li.find(sel)
            if cand and _clean_text(cand.get_text()):
                key_node = cand; break
        if key_node:
            label = _clean_text(key_node.get_text())
            value_texts = [_clean_text(a.get_text()) for a in li.find_all("a") if _clean_text(a.get_text())]
            if not value_texts:
                txt = _clean_text(li.get_text(" ", strip=True))
                if label and txt.lower().startswith(label.lower()):
                    txt = _clean_text(txt[len(label):])
                if txt:
                    value_texts = [txt]
            value = _clean_text(", ".join(value_texts))
            if label and value:
                pairs.append({"label": label, "value": value})
    for block in soup.select(".book-information, .info, .meta, .desc, .book-meta, .truyen-info"):
        for row in block.find_all(["p", "div"]):
            txt = _clean_text(row.get_text(" ", strip=True))
            if not txt or ":" not in txt:
                continue
            label, value = txt.split(":", 1)
            label, value = _clean_text(label), _clean_text(value)
            if label and value:
                pairs.append({"label": label, "value": value})
    return pairs


def _match_value(pairs: List[Dict[str, str]], key: str) -> Optional[str]:
    if key not in LABEL_KEYS:
        return None
    keywords = LABEL_KEYS[key]
    for p in pairs:
        lab = _clean_text(p.get("label")); val = _clean_text(p.get("value"))
        if not lab or not val:
            continue
        if any(k in lab.lower() for k in keywords):
            return val
    return None


def _extract_from_bookinfo(soup: BeautifulSoup):
    box = soup.select_one(".book-information .book-info p.tag") or soup.select_one(".book-information .book-info p.tag")
    if not box:
        return None, None, []
    author = None; status = None; genres: List[str] = []
    for a in box.find_all("a", href=True):
        href = a["href"].lower(); text = _clean_text(a.get_text())
        if not text:
            continue
        if "tac-gia" in href or "author" in href:
            author = text
    sp = box.find("span")
    if sp:
        status = _clean_text(sp.get_text())
    for a in box.find_all("a", href=True):
        href = a["href"].lower(); text = _clean_text(a.get_text())
        if text and "/the-loai/" in href:
            genres.append(text)
    genres = list(dict.fromkeys(genres))
    return author or None, status or None, genres


def _extract_genres_as_list(value: Optional[str], soup: BeautifulSoup) -> List[str]:
    res: List[str] = []
    if not value:
        node = soup.find(lambda tag: tag.name in ["div", "li", "p", "tr", "dl"]
                         and "thể loại" in _clean_text(tag.get_text()).lower())
        if node:
            for a in node.find_all("a"):
                t = _clean_text(a.get_text())
                if t and t not in res:
                    res.append(t)
        return res
    parts = re.split(r"[,/|·•;–\-–]+", value)
    for p in parts:
        p = _clean_text(p)
        if p and p not in ["—", "-"]:
            res.append(p)
    return list(dict.fromkeys(res))

# ===================== TOC ONE-SHOT =====================
# Lấy story_id từ trang truyện, sau đó gọi /story/chapters?story_id=...

_CHAP_RE = re.compile(r"(?:chuong[-\s]*|chapter[-\s]*)(\d+)", flags=re.I)


def _parse_chapter_no(text: str, href: str) -> Optional[int]:
    m = re.search(r"chương\s*(\d+)", text, flags=re.I)
    if m:
        return int(m.group(1))
    m = _CHAP_RE.search(href)
    if m:
        return int(m.group(1))
    return None


def _extract_story_id(soup: BeautifulSoup, sess: requests.Session) -> Tuple[Optional[str], Optional[str]]:
    """
    Trích xuất story_id và một chapter_id.
    Đây là logic phức tạp nhất vì TTV có nhiều cách lưu trữ:
    1.  Tìm story_id trong input/script.
    2.  Nếu không có, tìm book_id trong meta tag.
    3.  Dùng book_id để gọi API /book/catalogs, từ đó lấy ra story_id.
    4.  Luôn tìm một chapter_id từ các thẻ <li>.
    """
    story_id = None
    chapter_id = None
    book_id = None

    # Cách 1: Tìm story_id trực tiếp trên trang
    node = soup.select_one('input[name="story_id"]')
    if node and node.get("value"):
        story_id = node.get("value").strip()
    
    if not story_id:
        txt = soup.get_text(" ", strip=True)
        m = re.search(r'story_id[^\d"]*["\':\s]+(\d{2,9})', txt)
        if m:
            story_id = m.group(1)

    # Cách 2: Nếu không có story_id, tìm book_id để gọi API
    if not story_id:
        meta_tag = soup.select_one('meta[name="book_detail"]')
        if meta_tag and meta_tag.get("content"):
            book_id = meta_tag["content"].strip()
            
        if book_id:
            print(f"Không tìm thấy story_id, thử lấy qua book_id: {book_id}")
            try:
                # Gọi API /book/catalogs để lấy story_id
                api_url = f"https://tangthuvien.net/book/catalogs?book_id={book_id}"
                resp = sess.get(api_url, timeout=TIMEOUT_AJAX, headers=HEADERS)
                data = resp.json()
                if data.get("data", {}).get("story_id"):
                    story_id = str(data["data"]["story_id"])
                    print(f"✓ Lấy được story_id từ API: {story_id}")
            except Exception as e:
                print(f"⚠️ Lỗi khi gọi API lấy story_id: {e}")

    # Luôn tìm một chapter_id để dùng làm tham số
    first_chap_li = soup.select_one('li[ng-chap], li[data-chap-id]')
    if first_chap_li:
        chapter_id = first_chap_li.get('ng-chap') or first_chap_li.get('data-chap-id')

    return story_id, chapter_id


# Đường dẫn API lấy danh sách chương
# Đã sửa lại để chỉ dùng story_id theo gợi ý của người dùng.
TOC_ONE_SHOT_PATH = "/story/chapters?story_id={sid}"


def _anchors_to_items_from_soup(soup: BeautifulSoup, base_url: str) -> List[Dict]:
    """
    Hàm này được TỐI ƯU để xử lý cả HTML từ trang truyện và HTML từ API trả về.
    """
    # Selector này bắt được link chương ở cả 2 trường hợp
    anchors = soup.select('li > a[href*="/chuong"]')
    out: List[Dict] = []
    
    for a in anchors:
        if not a.has_attr("href") or "javascript:void(0)" in a["href"]:
            # Bỏ qua các link giả như chương đang đọc
            continue
            
        title = _clean_text(a.get_text())
        href = a["href"].strip()
        
        # Chuẩn hóa href
        if href.startswith("//"):
            href = "https:" + href
        
        full_url = urljoin(base_url, href)
        
        out.append({
            "title": title,
            "url": full_url,
            "chap_no": _parse_chapter_no(title, href),
        })
    return out


def _fetch_full_toc_via_api(base_url: str, story_id: str, chapter_id: Optional[str], sess: requests.Session) -> List[Dict]:
    """Tải toàn bộ danh sách chương bằng API, sử dụng story_id."""
    last_err = None
    for host in MIRRORS:
        # Đã thay đổi: Loại bỏ chapter_id, chỉ dùng story_id.
        url = f"https://{host}{TOC_ONE_SHOT_PATH.format(sid=story_id)}" 
        try:
            r = sess.get(url, headers={"Referer": base_url, **HEADERS}, timeout=TIMEOUT_HTML, verify=not INSECURE_SSL)
            if r.status_code != 200 or len(r.text) < 500:
                last_err = RuntimeError(f"{host} bad status/short body: {r.status_code}")
                continue
                
            doc = BeautifulSoup(r.text, "html.parser")
            # API trả về một list <ul>, chúng ta chỉ cần xử lý nó
            items = _anchors_to_items_from_soup(doc, base_url)
            if items:
                print(f"✓ Lấy mục lục thành công qua API ({host}), tìm thấy {len(items)} chương.")
                return items
            last_err = RuntimeError(f"{host} no anchors found in API response")
        except requests.RequestException as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    return []



# ===================== TOC FALLBACK (AJAX hội tụ) =====================
# ... (Phần code AJAX crawl không dùng trong logic mới, nhưng giữ lại)

def _anchors_to_items_from_html(html_text: str, base_url: str) -> List[Dict]:
    s = BeautifulSoup(html_text, "html.parser")
    return _anchors_to_items_from_soup(s, base_url)


def _guess_last_page_from_dom(dom: BeautifulSoup) -> Optional[int]:
    nums = []
    for a in dom.select("ul.pagination a"):
        oc = a.get("onclick", "")
        m = re.search(r"Loading\((\d+)\)", oc)
        if m:
            nums.append(int(m.group(1)))
        t = _clean_text(a.get_text())
        if t.isdigit():
            nums.append(int(t))
    return max(nums) if nums else None


def _ajax_url(book_id: str, page: int, host: str) -> str:
    return f"https://{host}/doc-truyen/page/{book_id}?page={page}&limit={AJAX_LIMIT}&web=1"


def _fetch_ajax_page(base_url: str, book_id: str, page: int, sess: requests.Session,
                     prefer_host: Optional[str], attempt: int) -> Optional[str]:
    hosts = [prefer_host] if prefer_host else []
    for h in MIRRORS:
        if h not in hosts:
            hosts.append(h)

    for host in hosts:
        url = _ajax_url(book_id, page, host)
        try:
            r = sess.get(
                url,
                timeout=TIMEOUT_AJAX,
                headers={
                    "Referer": base_url,
                    "X-Requested-With": "XMLHttpRequest",
                    **HEADERS,
                },
                verify=not INSECURE_SSL,
            )
            if r.status_code != 200:
                continue
            text = r.text or ""
            if ("/chuong" not in text.lower()) or (len(text) < 512):
                continue
            return text
        except requests.RequestException:
            continue

    sleep_s = (AJAX_BACKOFF_BASE * (attempt + 1)) + random.uniform(*AJAX_JITTER)
    time.sleep(sleep_s)
    return None

# ===================== EXTRACT CHAPTER LIST =====================

def _extract_chapters(soup: BeautifulSoup, base_url: str) -> Tuple[List[Dict], Dict[str, Optional[int]]]:
    """
    Logic lấy danh sách chương cuối cùng, ổn định và chính xác.
    Đã sửa để ưu tiên gọi API chỉ với story_id.
    """
    chapters: List[Dict] = []
    sess = _make_session()
    
    # 1) Cố gắng lấy danh sách chương qua API (cách tốt nhất)
    story_id, chapter_id = _extract_story_id(soup, sess)
    
    if story_id: # In ra story_id để người dùng kiểm tra
        print(f"✓ Tìm thấy Story ID: {story_id}")

    if story_id: # Chỉ cần story_id để gọi API
        try:
            # GỌI API chỉ với story_id (chapter_id là None, bị bỏ qua trong hàm _fetch_full_toc_via_api đã sửa)
            chapters = _fetch_full_toc_via_api(base_url, story_id, None, sess) 
        except Exception as e:
            print(f"⚠️ Không thể lấy danh sách chương qua API: {e}")
            print("→ Chuyển sang lấy các chương có sẵn trên trang đầu tiên.")
            pass
    else:
        print("Không tìm thấy đủ Story ID.")
    
    # 2) Nếu API thất bại, chỉ lấy những chương có sẵn trên trang truyện
    if not chapters:
        chapters = _anchors_to_items_from_soup(soup, base_url)

    # Dedupe + sort để đảm bảo dữ liệu sạch
    uniq_map = {}
    for c in chapters:
        # Dùng URL làm key để loại bỏ trùng lặp
        if c.get("url"):
            uniq_map[c["url"]] = c
            
    unique = list(uniq_map.values())
    unique.sort(key=lambda x: (999999 if x.get("chap_no") is None else x["chap_no"], x.get("title") or ""))
    
    crawl_stats = {"pages_crawled": 1, "guessed_last_page": None}
    print(f"🔗 Collected chapter links: {len(unique)} (Method: {'API' if story_id and chapters else 'HTML Only'})")

    return unique, crawl_stats


# ===================== GET CHAPTER CONTENT =====================
_CONTENT_SELECTORS = [
    ".chapter-c-content .box-chap",
    ".chapter-c .box-chap",
    "div.box-chap",
    ".book-content-wrap .content",
    ".book-content-wrap #bookContent",
    ".book-content-wrap .book-content",
    ".read-content", ".content-wrap",
    "#bookContent", ".book-content",
]
_REMOVE_INSIDE = [
    ".box-adv", ".ads", ".adsbox", ".advertisement", ".banner",
    ".share", ".like", ".social", ".fb", "ins", "iframe",
    ".chapter-nav", ".chapter-control", ".tool-box", ".control-box",
    ".left-control", ".right-control",
    "#list-comment", ".list-comment", ".comment", "#comments",
    "script", "noscript", "style",
]


def _extract_chapter_title(soup: BeautifulSoup) -> str:
    for sel in [
        ".chapter-c-content h5 a", ".chapter-c-content h5",
        ".chapter-c-content h4 a", ".chapter-c-content h4",
    ]:
        n = soup.select_one(sel)
        if n and _clean_text(n.get_text()):
            return _clean_text(n.get_text())
    for sel in [
        ".book-content_wrap h1", ".book-content-wrap h1", ".book-content-wrap h2",
        "h1", "h2",
    ]:
        n = soup.select_one(sel)
        if n and _clean_text(n.get_text()):
            return _clean_text(n.get_text())
    t = soup.find("title")
    if t:
        return re.split(r"\s+[-–]\s+", _clean_text(t.get_text()), 1)[0]
    return "Chương ?"


def _extract_content_boxes(soup: BeautifulSoup) -> List[BeautifulSoup]:
    boxes: List[BeautifulSoup] = []
    for sel in _CONTENT_SELECTORS:
        found = soup.select(sel)
        if found:
            boxes.extend(found)
            break
    cleaned: List[BeautifulSoup] = []
    for b in boxes:
        for rm in _REMOVE_INSIDE:
            for bad in b.select(rm):
                bad.decompose()
        if _clean_text(b.get_text()):
            cleaned.append(b)
    return cleaned


def _box_has_structured_breaks(box: BeautifulSoup) -> bool:
    if box.find("p") or box.find("br"):
        return True
    if box.find(["blockquote", "li"]):
        return True
    children_divs = box.find_all("div", recursive=False)
    if len(children_divs) >= 2:
        return True
    return False


def _render_box_as_html(box: BeautifulSoup) -> str:
    if _box_has_structured_breaks(box):
        return str(box)
    raw = box.get_text("\n", strip=True)
    paras = [p.strip() for p in re.split(r"\n\s*\n+", raw) if p.strip()]
    parts = [f"<p>{html.escape(p)}</p>" for p in paras]
    if not parts:
        sentences = re.split(r"(?<=[\.\!\?…])\s+", raw)
        buf, chunk, chunks = [], 0, []
        for s in sentences:
            if not s.strip():
                continue
            buf.append(s.strip()); chunk += 1
            if chunk >= 3:
                chunks.append(" ".join(buf)); buf, chunk = [], 0
        if buf:
            chunks.append(" ".join(buf))
        parts = [f"<p>{html.escape(x)}</p>" for x in chunks] if chunks else [f"<p>{html.escape(raw)}</p>"]
    return "\n".join(parts)


def get_chapter(url: str) -> Dict:
    soup = _fetch_html(url)
    title = _extract_chapter_title(soup)
    boxes = _extract_content_boxes(soup)
    if not boxes:
        return {"title": title, "url": url, "content_html": "", "text": ""}
    html_chunks = [_render_box_as_html(b) for b in boxes]
    content_html = "\n".join(html_chunks)
    text = _clean_text(BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True))
    return {
        "title": title,
        "url": url,
        "content_html": content_html,
        "text": text,
    }

# ===================== COVER =====================
import requests as _rq

def _fetch_cover_from_book_page(book_page_url: str):
    soup = _fetch_html(book_page_url)
    img = (
        soup.select_one(".book-information .book-img img")
        or soup.select_one(".book-img img")
        or soup.select_one("img[itemprop='image']")
        or soup.select_one("img#bookImg")
    )
    if img and img.get("src"):
        src = urljoin(book_page_url, img["src"])
        r = _rq.get(src, headers=HEADERS, timeout=TIMEOUT_HTML)
        r.raise_for_status()
        content = r.content
        ct = r.headers.get("Content-Type", "").lower()
        ext = ".png" if ("png" in ct or src.lower().endswith(".png")) else ".jpg"
        return content, ext, src
    return None, None, None

# ===================== PUBLIC API =====================

def getText(url: str) -> Dict:
    soup = _fetch_html(url)

    title = _pick_title(soup) or "Truyện"
    author, status, genres = _extract_from_bookinfo(soup)

    if not (author and status and genres):
        pairs = _gather_label_value_pairs(soup)
        if not author:
            author = _match_value(pairs, "author")
        if not status:
            status = _match_value(pairs, "status")
        if not genres:
            genres_raw = _match_value(pairs, "genres")
            genres = _extract_genres_as_list(genres_raw, soup)

    if not author:
        cand = (
            soup.select_one('a[rel="author"]')
            or soup.select_one('[itemprop="author"]')
            or soup.select_one(".author a")
            or soup.select_one(".author")
        )
        if cand:
            author = _clean_text(cand.get_text())

    if not status:
        txt = soup.get_text(" ", strip=True).lower()
        m = re.search(r"(đang ra|đã hoàn thành|hoàn thành|tạm dừng)", txt)
        if m:
            status = _clean_text(m.group(0).title())

    total_chapters = None
    catalog_label = soup.select_one('#j-bookCatalogPage')
    if catalog_label:
        txt = _clean_text(catalog_label.get_text())
        m = re.search(r"\((\d+)\s*chương", txt, flags=re.I)
        if m:
            total_chapters = int(m.group(1))

    chapters, crawl_stats = _extract_chapters(soup, url)

    if total_chapters is None and chapters:
        total_chapters = len(chapters)

    return {
        "title": title,
        "author": author or None,
        "genres": genres or [],
        "status": status or None,
        "total_chapters": total_chapters,
        "chapters": chapters,
        "chapter_link_count": len(chapters),
        "pages_crawled": crawl_stats.get("pages_crawled", 1),
        "guessed_last_page": crawl_stats.get("guessed_last_page"),
    }


def save_all_chapters_to_html(
    book_title: str,
    chapters: List[Dict],
    out_dir: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
):
    os.makedirs(out_dir, exist_ok=True)
    if not chapters:
        return

    s = (start or 1) - 1
    e = end if end is not None else len(chapters)
    subset = chapters[s:e]

    idx_items = []
    # pad = len(str(len(chapters)))  # << BỎ DÒNG NÀY ĐI

    selected_total = len(subset)
    for done, (i, ch) in enumerate(enumerate(subset, start=s + 1), 1):
        info = get_chapter(ch["url"])
        fname = f"{i:04d}.html"  # << SỬA LẠI DÒNG NÀY ĐỂ LUÔN CÓ 4 CHỮ SỐ
        fpath = os.path.join(out_dir, fname)

        html_doc = f"""<!doctype html>
<html lang=\"vi\">
<meta charset=\"utf-8\">
<title>{html.escape(info['title'])}</title>
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;line-height:1.7;
      padding:24px;max-width:860px;margin:0 auto;}}
h1{{font-size:1.6rem;margin:0 0 1rem}}
.chapter{{margin-top:1rem; white-space: pre-wrap;}}
.chapter p{{margin:0 0 1rem}}
</style>
<h1>{html.escape(info['title'])}</h1>
<div class=\"chapter\">{info['content_html'] or '<p><i>(Không tìm thấy nội dung)</i></p>'}</div>
</html>"""
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(html_doc)

        idx_items.append(f'<li><a href="{fname}">' + html.escape(info["title"]) + "</a></li>")
        print(chapter_log_line(done, selected_total, info.get("status_code", 200), i, len(chapters), info.get("title") or ch.get("title") or ""))

    index_html = f"""<!doctype html>
<html lang=\"vi\">
<meta charset=\"utf-8\">
<title>{html.escape(book_title)} — Mục lục</title>
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;line-height:1.7;
      padding:24px;max-width:860px;margin:0 auto;}}
h1{{font-size:1.8rem;margin:0 0 1rem}}
ol{{padding-left:1.25rem}}
</style>
<h1>{html.escape(book_title)}</h1>
<ol>
{''.join(idx_items)}
</ol>
</html>"""
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)
    print("📖 Index: index.html")


def fetch_chapter_content(url: str) -> dict:
    return get_chapter(url)
