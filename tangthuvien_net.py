# -*- coding: utf-8 -*-
# TangThuVien extractor:
#   - https://tangthuvien.net/
#   - https://truyen.tangthuvien.vn/
#
# Lấy: title, author, genres(list), status, total_chapters, chapters[]
# + get_chapter(url) trả nội dung chương (gộp nhiều box-chap)
# + save_all_chapters_to_html(...) lưu toàn bộ thành HTML

from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, urljoin
import requests, re, os, html, unicodedata, ssl
from typing import List, Dict, Optional, Tuple
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

# -------- CONFIG --------
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36")
}
TIMEOUT = 30
INSECURE_SSL = False  # True -> verify=False (không khuyến nghị)

MIRRORS = ["tangthuvien.net", "truyen.tangthuvien.vn"]

# -------- TLS 1.2 Adapter --------
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
        total=3, connect=3, read=3, backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"], raise_on_status=False,
    )
    adapter = TLS12HttpAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s

# =============== UTIL ===============
def _clean_text(s: Optional[str]) -> str:
    if not s: return ""
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
    candidate_domains = [first_host] + [d for d in MIRRORS if d != first_host]
    last_err = None

    for host in candidate_domains:
        try:
            test_url = _with_domain(url, host)
            resp = sess.get(test_url, timeout=TIMEOUT, verify=not INSECURE_SSL)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.exceptions.RequestException as e:
            last_err = e
            continue

    if not INSECURE_SSL and last_err:
        try:
            resp = sess.get(url, timeout=TIMEOUT, verify=False)
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

# =============== TRÍCH NHÃN META ===============
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
            if label and value: pairs.append({"label": label, "value": value})
    for tr in soup.find_all("tr"):
        th = tr.find("th") or tr.find("td")
        tds = tr.find_all("td")
        if th and len(tds) >= 1:
            label = _clean_text(th.get_text())
            value_nodes = tds[1:] if len(tds) > 1 else tds
            value = _clean_text(" ".join(v.get_text(" ", strip=True) for v in value_nodes))
            if label and value: pairs.append({"label": label, "value": value})
    for li in soup.find_all("li"):
        key_node = None
        for sel in ["span", "b", "strong", "h3", "label", "em"]:
            cand = li.find(sel)
            if cand and _clean_text(cand.get_text()):
                key_node = cand; break
        if key_node:
            label = _clean_text(key_node.get_text())
            value_texts = [ _clean_text(a.get_text()) for a in li.find_all("a") if _clean_text(a.get_text()) ]
            if not value_texts:
                txt = _clean_text(li.get_text(" ", strip=True))
                if label and txt.lower().startswith(label.lower()):
                    txt = _clean_text(txt[len(label):])
                if txt: value_texts = [txt]
            value = _clean_text(", ".join(value_texts))
            if label and value: pairs.append({"label": label, "value": value})
    for block in soup.select(".book-information, .info, .meta, .desc, .book-meta, .truyen-info"):
        for row in block.find_all(["p", "div"]):
            txt = _clean_text(row.get_text(" ", strip=True))
            if not txt or ":" not in txt: continue
            label, value = txt.split(":", 1)
            label, value = _clean_text(label), _clean_text(value)
            if label and value: pairs.append({"label": label, "value": value})
    return pairs

def _match_value(pairs: List[Dict[str, str]], key: str) -> Optional[str]:
    if key not in LABEL_KEYS: return None
    keywords = LABEL_KEYS[key]
    for p in pairs:
        lab = _clean_text(p.get("label")); val = _clean_text(p.get("value"))
        if not lab or not val: continue
        if any(k in lab.lower() for k in keywords):
            return val
    return None

def _extract_from_bookinfo(soup: BeautifulSoup):
    box = soup.select_one(".book-info p.tag") or soup.select_one(".book-information .book-info p.tag")
    if not box: return None, None, []
    author = None; status = None; genres: List[str] = []
    for a in box.find_all("a", href=True):
        href = a["href"].lower(); text = _clean_text(a.get_text())
        if not text: continue
        if "tac-gia" in href or "author" in href: author = text
    sp = box.find("span")
    if sp: status = _clean_text(sp.get_text())
    for a in box.find_all("a", href=True):
        href = a["href"].lower(); text = _clean_text(a.get_text())
        if text and "/the-loai/" in href: genres.append(text)
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
                if t and t not in res: res.append(t)
        return res
    parts = re.split(r"[,/|·•;–\-–]+", value)
    for p in parts:
        p = _clean_text(p)
        if p and p not in ["—", "-"]: res.append(p)
    return list(dict.fromkeys(res))

# =============== DANH SÁCH CHƯƠNG ===============
_CHAP_RE = re.compile(r"(?:chuong[-\s]*|chapter[-\s]*)(\d+)", flags=re.I)

def _parse_chapter_no(text: str, href: str) -> Optional[int]:
    m = re.search(r"chương\s*(\d+)", text, flags=re.I)
    if m: return int(m.group(1))
    m = _CHAP_RE.search(href)
    if m: return int(m.group(1))
    return None

def _extract_chapters(soup: BeautifulSoup, base_url: str) -> Tuple[List[Dict], Dict[str, Optional[int]]]:
    """
    Thu toàn bộ danh sách chương.
    - Trang đầu: lấy từ HTML gốc (page=0)
    - AJAX: /doc-truyen/page/<book_id>?page=N&limit=75&web=1  với N bắt đầu từ 1
    Trả về (chapters, stats)
    """
    # --- book_id từ meta ---
    book_id = None
    meta_tag = soup.select_one('meta[name="book_detail"]')
    if meta_tag and meta_tag.get("content"):
        book_id = meta_tag["content"].strip()

    # --- parse 1 trang HTML danh sách chương ---
    def parse_chapter_page(html_text: str) -> List[Dict]:
        s = BeautifulSoup(html_text, "html.parser")
        anchors = s.select('.catalog-content-wrap a[href*="/chuong"]') or s.select('a[href*="/chuong"]')
        out = []
        for a in anchors:
            title = _clean_text(a.get_text())
            href = a.get("href", "")
            if not href:
                continue
            url = urljoin(base_url, href)
            chap_no = _parse_chapter_no(title, href)
            out.append({"title": title, "url": url, "chap_no": chap_no})
        return out

    # --- Trang đầu (page=0) ---
    chapters = parse_chapter_page(str(soup))
    pages_crawled = 1  # đã tính trang đầu

    # --- Đoán last_page từ DOM phân trang ---
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
        # ví dụ hiển thị 1..9 => last_page = 9 (AJAX cần gọi 1..8)
        return max(nums) if nums else None

    last_page = _guess_last_page_from_dom(soup)

    # --- Phân trang AJAX (page bắt đầu từ 1) ---
    if book_id:
        sess = _make_session()
        # Nếu biết last_page: gọi 1..(last_page-1); nếu không: tăng dần tới khi hết
        page = 1
        hard_stop = last_page is not None
        max_page = (last_page - 1) if hard_stop else 10**9

        prev_total = len(chapters)
        while page <= max_page:
            api_url = f"https://truyen.tangthuvien.vn/doc-truyen/page/{book_id}?page={page}&limit=75&web=1"
            try:
                r = sess.get(
                    api_url,
                    timeout=15,
                    headers={"Referer": base_url, "X-Requested-With": "XMLHttpRequest"},
                    verify=not INSECURE_SSL,
                )
                if r.status_code != 200:
                    break
                if "/chuong" not in r.text.lower():
                    break

                new_items = parse_chapter_page(r.text)
                if not new_items:
                    break

                chapters.extend(new_items)
                pages_crawled += 1
                print(f"📄 Loaded page {page}/{(last_page - 1) if hard_stop else '?'} — total: {len(chapters)}")

                if not hard_stop:
                    if len(chapters) == prev_total:
                        break
                    prev_total = len(chapters)
                    frag = BeautifulSoup(r.text, "html.parser")
                    lp2 = _guess_last_page_from_dom(frag)
                    if lp2 and lp2 != last_page:
                        last_page = lp2
                        max_page = last_page - 1  # AJAX: 1..last-1

                page += 1
            except Exception as e:
                print(f"⚠️ Stop at page {page}: {e}")
                break

    # --- Lọc trùng & sắp xếp ---
    seen = set()
    unique: List[Dict] = []
    for c in chapters:
        key = (c["url"], c["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    unique.sort(key=lambda x: (999999 if x["chap_no"] is None else x["chap_no"], x["title"]))
    print(f"🔗 Collected chapter links: {len(unique)} (pages crawled: {pages_crawled}, last_page label: {last_page or '?'})")
    return unique, {"pages_crawled": pages_crawled, "guessed_last_page": last_page}

# =============== LẤY NỘI DUNG CHƯƠNG ===============
# Ưu tiên đúng layout site:
#   .chapter-c-content > .box-chap.box-chap-xxxx
# + dự phòng các layout phổ biến khác.
_CONTENT_SELECTORS = [
    ".chapter-c-content .box-chap",
    ".chapter-c .box-chap",
    'div[class*="box-chap box-chap-"]',
    ".book-content-wrap .content",
    ".book-content-wrap #bookContent",
    ".book-content-wrap .book-content",
    ".read-content", ".content-wrap",
    "#bookContent", ".book-content",
]

# Loại bỏ rác trong vùng nội dung
_REMOVE_INSIDE = [
    ".box-adv", ".ads", ".adsbox", ".advertisement", ".banner",
    ".share", ".like", ".social", ".fb", "ins", "iframe",
    ".chapter-nav", ".chapter-control", ".tool-box", ".control-box",
    ".left-control", ".right-control",
    "#list-comment", ".list-comment", ".comment", "#comments",
    "script", "noscript", "style",
]

def _extract_chapter_title(soup: BeautifulSoup) -> str:
    for sel in [".chapter-c-content h5 a", ".chapter-c-content h5",
                ".chapter-c-content h4 a", ".chapter-c-content h4"]:
        n = soup.select_one(sel)
        if n and _clean_text(n.get_text()):
            return _clean_text(n.get_text())
    for sel in [".book-content-wrap h1", ".book-content-wrap h2", "h1", "h2"]:
        n = soup.select_one(sel)
        if n and _clean_text(n.get_text()):
            return _clean_text(n.get_text())
    t = soup.find("title")
    if t:
        return re.split(r"\s+[-–]\s+", _clean_text(t.get_text()), 1)[0]
    return "Chương ?"

def _extract_content_boxes(soup: BeautifulSoup) -> List[BeautifulSoup]:
    """Lấy tất cả box nội dung hợp lệ theo thứ tự."""
    boxes: List[BeautifulSoup] = []
    for sel in _CONTENT_SELECTORS:
        found = soup.select(sel)
        if found:
            boxes.extend(found)
            break  # dùng nhóm đầu khớp
    # lọc rác & bỏ box rỗng
    cleaned: List[BeautifulSoup] = []
    for b in boxes:
        for rm in _REMOVE_INSIDE:
            for bad in b.select(rm):
                bad.decompose()
        if _clean_text(b.get_text()):
            cleaned.append(b)
    return cleaned

def _box_has_structured_breaks(box: BeautifulSoup) -> bool:
    """Box đã có <p>/<br> hoặc khối block rõ ràng thì giữ nguyên."""
    if box.find("p") or box.find("br"):
        return True
    if box.find(["blockquote", "li"]):
        return True
    # nhiều div con: thường mỗi div là 1 đoạn
    children_divs = box.find_all("div", recursive=False)
    if len(children_divs) >= 2:
        return True
    return False

def _render_box_as_html(box: BeautifulSoup) -> str:
    """
    - Nếu box đã có <p>/<br>/div con → trả nguyên HTML (giữ <br> để xuống dòng).
    - Nếu KHÔNG có → biến text thành các <p> theo khoảng trắng trống.
    """
    if _box_has_structured_breaks(box):
        return str(box)

    raw = box.get_text("\n", strip=True)
    paras = [p.strip() for p in re.split(r"\n\s*\n+", raw) if p.strip()]
    parts = [f"<p>{html.escape(p)}</p>" for p in paras]
    # nếu cũng không có dòng trống, fallback: cắt mềm theo dấu câu mỗi ~2-3 câu
    if not parts:
        sentences = re.split(r"(?<=[\.!\?…])\s+", raw)
        buf, chunk, chunks = [], 0, []
        for s in sentences:
            if not s.strip(): continue
            buf.append(s.strip()); chunk += 1
            if chunk >= 3:
                chunks.append(" ".join(buf)); buf, chunk = [], 0
        if buf: chunks.append(" ".join(buf))
        parts = [f"<p>{html.escape(x)}</p>" for x in chunks] if chunks else [f"<p>{html.escape(raw)}</p>"]
    return "\n".join(parts)

def get_chapter(url: str) -> Dict:
    """
    Trả về:
    {
      'title': 'Chương 1: ...',
      'url': 'https://...',
      'content_html': '<div>....</div>',
      'text': 'nội dung thuần văn bản'
    }
    """
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

# --- COVER: tải ảnh bìa từ trang sách (TangThuVien) ---
def _fetch_cover_from_book_page(book_page_url: str):
    soup = _fetch_html(book_page_url)
    img = (soup.select_one(".book-information .book-img img")
           or soup.select_one(".book-img img")
           or soup.select_one("img[itemprop='image']")
           or soup.select_one("img#bookImg"))
    if img and img.get("src"):
        src = urljoin(book_page_url, img["src"])
        r = requests.get(src, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        content = r.content
        ct = r.headers.get("Content-Type","").lower()
        ext = ".png" if ("png" in ct or src.lower().endswith(".png")) else ".jpg"
        return content, ext, src
    return None, None, None


# =============== API CHÍNH ===============
def getText(url: str) -> Dict:
    soup = _fetch_html(url)

    title = _pick_title(soup) or "Truyện"

    # Meta ưu tiên block gọn:
    author, status, genres = _extract_from_bookinfo(soup)

    if not (author and status and genres):
        pairs = _gather_label_value_pairs(soup)
        if not author: author = _match_value(pairs, "author")
        if not status: status = _match_value(pairs, "status")
        if not genres:
            genres_raw = _match_value(pairs, "genres")
            genres = _extract_genres_as_list(genres_raw, soup)

    if not author:
        cand = (soup.select_one('a[rel="author"]')
                or soup.select_one('[itemprop="author"]')
                or soup.select_one(".author a")
                or soup.select_one(".author"))
        if cand: author = _clean_text(cand.get_text())

    if not status:
        txt = soup.get_text(" ", strip=True).lower()
        m = re.search(r"(đang ra|đã hoàn thành|hoàn thành|tạm dừng)", txt)
        if m: status = _clean_text(m.group(0).title())

    # Số chương ở label tab
    total_chapters = None
    catalog_label = soup.select_one('#j-bookCatalogPage')
    if catalog_label:
        txt = _clean_text(catalog_label.get_text())
        m = re.search(r"\((\d+)\s*chương", txt, flags=re.I)
        if m: total_chapters = int(m.group(1))

    chapters, crawl_stats = _extract_chapters(soup, url)
    if total_chapters is None and chapters:
        total_chapters = len(chapters)

    return {
        "title": title,
        "author": author or None,
        "genres": genres or [],
        "status": status or None,
        "total_chapters": total_chapters,
        "chapters": chapters,  # [{title, url, chap_no}]
        # thống kê phục vụ kiểm thử
        "chapter_link_count": len(chapters),
        "pages_crawled": crawl_stats.get("pages_crawled", 1),
        "guessed_last_page": crawl_stats.get("guessed_last_page"),
    }

# ===================== LƯU TOÀN BỘ CHƯƠNG THÀNH HTML =====================
def save_all_chapters_to_html(book_title: str, chapters: List[Dict], out_dir: str,
                              start: Optional[int] = None, end: Optional[int] = None):
    """
    Lưu từng chương → 0001.html, 0002.html ... + tạo index.html
    - chapters: [{title, url, chap_no}]
    - có thể giới hạn start/end (theo chỉ số 1-based trong list đã sắp xếp)
    """
    os.makedirs(out_dir, exist_ok=True)
    if not chapters:
        return

    s = (start or 1) - 1
    e = end if end is not None else len(chapters)
    subset = chapters[s:e]

    idx_items = []
    pad = len(str(len(chapters)))

    for i, ch in enumerate(subset, start=s+1):
        info = get_chapter(ch["url"])
        fname = f"{str(i).zfill(pad)}.html"
        fpath = os.path.join(out_dir, fname)

        html_doc = f"""<!doctype html>
<html lang="vi">
<meta charset="utf-8">
<title>{html.escape(info['title'])}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;line-height:1.7;
      padding:24px;max-width:860px;margin:0 auto;}}
h1{{font-size:1.6rem;margin:0 0 1rem}}
.chapter{{margin-top:1rem; white-space: pre-wrap;}}
.chapter p{{margin:0 0 1rem}}
</style>
<h1>{html.escape(info['title'])}</h1>
<div class="chapter">{info['content_html'] or '<p><i>(Không tìm thấy nội dung)</i></p>'}</div>
</html>"""
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(html_doc)

        idx_items.append(f'<li><a href="{fname}">{html.escape(info["title"])}</a></li>')
        print(f"✔ Saved: {fname} — {info['title']}")

    index_html = f"""<!doctype html>
<html lang="vi">
<meta charset="utf-8">
<title>{html.escape(book_title)} — Mục lục</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
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
    print(f"📖 Index: index.html")

def fetch_chapter_content(url: str) -> dict:
    """Alias cho get_chapter để dùng với epub_builder."""
    return get_chapter(url)
