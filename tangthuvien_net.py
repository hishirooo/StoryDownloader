# -*- coding: utf-8 -*-
# TangThuVien meta extractor (4 field): title, author, genres, status
# Mirrors: https://tangthuvien.net/, https://truyen.tangthuvien.vn/

from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse
import requests, re, os, html, unicodedata, ssl
from typing import List, Dict, Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

# -------- CONFIG --------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
TIMEOUT = 30
# Chỉ bật nếu bạn biết mình đang làm gì (bỏ qua xác thực chứng chỉ)
INSECURE_SSL = False  # True -> verify=False

# Mirror thứ tự ưu tiên khi gặp lỗi TLS/SSL
MIRRORS = ["tangthuvien.net", "truyen.tangthuvien.vn"]

# -------- TLS 1.2 Adapter (tránh SSLEOFError do handshake) --------
class TLS12HttpAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        # ép tối thiểu TLSv1.2
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except Exception:
            # fallback kiểu cũ (ít gặp trên Python mới)
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
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = TLS12HttpAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s

# =============== UTIL ===============
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
    """
    Tải HTML với:
      - TLS 1.2 adapter
      - retry
      - fallback mirror nếu lỗi SSL
      - tùy chọn INSECURE_SSL để verify=False (chỉ khi cần)
    """
    sess = _make_session()

    # Thử domain hiện tại trước; nếu fail SSL, thử các mirror còn lại
    tried = []
    first_host = urlparse(url).netloc.lower()
    candidate_domains = [first_host] + [d for d in MIRRORS if d != first_host]

    last_err = None
    for host in candidate_domains:
        try:
            tried.append(host)
            test_url = _with_domain(url, host)
            resp = sess.get(test_url, timeout=TIMEOUT, verify=not INSECURE_SSL)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.exceptions.SSLError as e:
            last_err = e
            # thử mirror tiếp theo
            continue
        except requests.exceptions.RequestException as e:
            last_err = e
            # nếu là lỗi khác (timeout/5xx), vẫn thử mirror tiếp
            continue

    # Thử lần cuối: nếu vẫn lỗi SSL và bạn CHẤP NHẬN rủi ro, tự động thử verify=False (nếu chưa bật)
    if not INSECURE_SSL and last_err:
        try:
            sess = _make_session()
            resp = sess.get(url, timeout=TIMEOUT, verify=False)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e2:
            raise last_err

    # Nếu tới đây vẫn lỗi
    if last_err:
        raise last_err
    raise RuntimeError("Không tải được trang (không rõ nguyên nhân)")

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

# =============== TRÍCH NHÃN ===============
LABEL_KEYS = {
    "author": ["tác giả", "tac gia", "tác-giả", "author"],
    "genres": ["thể loại", "the loai", "thể-loại", "genre", "thể loại(s)"],
    "status": ["tình trạng", "trạng thái", "tinh trang", "trang thai", "status"],
}

def _gather_label_value_pairs(soup: BeautifulSoup) -> List[Dict[str, str]]:
    pairs = []

    # 1) dl/dt/dd
    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label = _clean_text(dt.get_text())
            value = _clean_text(dd.get_text(" ", strip=True))
            if label and value:
                pairs.append({"label": label, "value": value})

    # 2) bảng
    for tr in soup.find_all("tr"):
        th = tr.find("th") or tr.find("td")
        tds = tr.find_all("td")
        if th and len(tds) >= 1:
            label = _clean_text(th.get_text())
            value_nodes = tds[1:] if len(tds) > 1 else tds
            value = _clean_text(" ".join(v.get_text(" ", strip=True) for v in value_nodes))
            if label and value:
                pairs.append({"label": label, "value": value})

    # 3) li + label
    for li in soup.find_all("li"):
        key_node = None
        for sel in ["span", "b", "strong", "h3", "label", "em"]:
            cand = li.find(sel)
            if cand and _clean_text(cand.get_text()):
                key_node = cand
                break
        if key_node:
            label = _clean_text(key_node.get_text())
            value_texts = []
            for a in li.find_all("a"):
                t = _clean_text(a.get_text())
                if t:
                    value_texts.append(t)
            if not value_texts:
                txt = _clean_text(li.get_text(" ", strip=True))
                if label and txt.lower().startswith(label.lower()):
                    txt = _clean_text(txt[len(label):])
                if txt:
                    value_texts = [txt]
            value = _clean_text(", ".join(value_texts))
            if label and value:
                pairs.append({"label": label, "value": value})

    # 4) các block phổ biến
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
        lab = _clean_text(p.get("label"))
        val = _clean_text(p.get("value"))
        if not lab or not val:
            continue
        low = lab.lower()
        if any(k in low for k in keywords):
            return val
    return None

def _extract_from_bookinfo(soup: BeautifulSoup):
    """
    Ưu tiên cấu trúc chuẩn của TangThuVien:
      <div class="book-info">
        <p class="tag">
          <a href="/tac-gia?...">TÁC GIẢ</a>
          <span class="blue|red">Đang ra|Hoàn thành|...</span>
          <a href="/the-loai/...">Thể loại 1</a>
          <a href="/the-loai/...">Thể loại 2</a>
        </p>
    Trả về (author, status, genres_list) hoặc (None, None, []) nếu không thấy.
    """
    box = soup.select_one(".book-info p.tag") or soup.select_one(".book-information .book-info p.tag")
    if not box:
        return None, None, []

    author = None
    status = None
    genres = []

    # Tác giả: a có href chứa 'tac-gia'
    for a in box.find_all("a", href=True):
        href = a["href"].lower()
        text = _clean_text(a.get_text())
        if not text:
            continue
        if "tac-gia" in href or "author" in href:
            author = text
            break  # thường chỉ 1

    # Tình trạng: span.blue/red (hoặc span bất kỳ)
    sp = box.find("span")
    if sp:
        status = _clean_text(sp.get_text())

    # Thể loại: tất cả a có href chứa '/the-loai/'
    for a in box.find_all("a", href=True):
        href = a["href"].lower()
        text = _clean_text(a.get_text())
        if text and "/the-loai/" in href:
            genres.append(text)

    # unique, giữ thứ tự
    genres = list(dict.fromkeys(genres))
    return author or None, status or None, genres




def _extract_genres_as_list(value: Optional[str], soup: BeautifulSoup) -> List[str]:
    res: List[str] = []
    if not value:
        node = soup.find(lambda tag: tag.name in ["div", "li", "p", "tr", "dl"] and "thể loại" in _clean_text(tag.get_text()).lower())
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




# =============== API CHÍNH ===============
def getText(url: str) -> Dict:
    soup = _fetch_html(url)

    title = _pick_title(soup) or "Truyện"

    # 1) Ưu tiên lấy gọn từ .book-info p.tag
    author, status, genres = _extract_from_bookinfo(soup)

    # 2) Thiếu gì thì fallback sang matcher tổng quát cũ
    if not (author and status and genres):
        pairs = _gather_label_value_pairs(soup)

        if not author:
            author = _match_value(pairs, "author")
        if not status:
            status = _match_value(pairs, "status")

        if not genres:
            genres_raw = _match_value(pairs, "genres")
            genres = _extract_genres_as_list(genres_raw, soup)

    # 3) Thêm một lớp dự phòng nữa cho author/status nếu còn thiếu
    if not author:
        cand = (soup.select_one('a[rel="author"]')
                or soup.select_one('[itemprop="author"]')
                or soup.select_one(".author a")
                or soup.select_one(".author"))
        if cand:
            author = _clean_text(cand.get_text())

    if not status:
        txt = soup.get_text(" ", strip=True).lower()
        m = re.search(r"(đang ra|đã hoàn thành|hoàn thành|tạm dừng)", txt)
        if m:
            status = _clean_text(m.group(0).title())
    # ====== Bắt số chương ======
    total_chapters = None
    catalog = soup.select_one('#j-bookCatalogPage')
    if catalog:
        txt = _clean_text(catalog.get_text())
        m = re.search(r"\((\d+)\s*chương", txt, flags=re.I)
        if m:
            total_chapters = int(m.group(1))
    return {
        "title": title,
        "author": author or None,
        "genres": genres or [],
        "status": status or None,
        "chapters": [],
        "total_chapters": total_chapters or None,
    }

