# -*- coding: utf-8 -*-
from __future__ import annotations
import re, time, html
from typing import List, Dict, Optional, Tuple, Callable
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

DEFAULT_TIMEOUT = 25
DEFAULT_LIMIT = 75
DEFAULT_DELAY = 0.35
INSECURE_SSL = False

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/141.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
}

# Nhận cả 2 kiểu URL chương: /chuong-123 và /185602-chuong-16 (có thể kèm ?query/#hash)
CHAPTER_HREF_RE = re.compile(
    r"/doc-truyen/[^/]+/(?:\d+-)?chuong-\d+(?:[/?#].*)?$",
    re.IGNORECASE,
)

# ----------------- helpers -----------------

def _clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s

def _norm_url(u: str) -> str:
    """Chuẩn hoá URL tuyệt đối để dedupe: bỏ query/hash, bỏ slash cuối."""
    p = urlparse(u)
    p2 = p._replace(query="", fragment="")
    return urlunparse(p2).rstrip("/")

def _pick_text_by_label(soup: BeautifulSoup, labels) -> str:
    for lab in labels:
        node = soup.find(string=re.compile(lab, re.I))
        if node:
            sib = getattr(node.parent, "find_next_sibling", lambda *a, **k: None)()
            txt = _clean(sib.get_text()) if sib else ""
            if txt:
                return txt
    return ""

def _find_cover_url(soup: BeautifulSoup, base_url: str) -> str:
    for sel in ["#bookImg img", ".book-img img", ".book-information .book-img img", "img[onerror*='default-book']"]:
        n = soup.select_one(sel)
        if n and n.get("src"):
            src = n["src"]
            if src.startswith("//"): src = "https:" + src
            return urljoin(base_url, src)
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        src = og["content"]
        if src.startswith("//"): src = "https:" + src
        return urljoin(base_url, src)
    return ""

def _guess_last_label_and_last_index(soup: BeautifulSoup) -> Tuple[Optional[int], Optional[int]]:
    """Trả về (last_label 1-based, last_index 0-based ở 'Trang cuối' nếu có)."""
    last_label = None
    nums = []
    for a in soup.select(".pagination li a"):
        t = a.get_text(strip=True)
        if t.isdigit():
            nums.append(int(t))
    if nums:
        last_label = max(nums)
    last_index = None
    for a in soup.select(".pagination li a"):
        txt = (a.get_text(strip=True) or "").lower()
        if "trang cuối" in txt or "cuối" in txt:
            oc = a.get("onclick") or ""
            m = re.search(r"Loading\((\d+)\)", oc)
            if m:
                last_index = int(m.group(1))
                break
    return last_label, last_index

def _parse_chapter_list(html_text: str, base: str) -> List[Dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    out: List[Dict] = []
    # rộng tay một chút để không bỏ sót
    for a in soup.select("ul.cf li a[href], a[href]"):
        href = a.get("href") or ""
        abs_href = urljoin(base, href.strip())
        if not CHAPTER_HREF_RE.search(abs_href):
            continue
        out.append({
            "url": _norm_url(abs_href),
            "title": _clean(a.get_text(" ")),
        })
    return out

def _extract_book_id_from_page(soup: BeautifulSoup, raw_html: str) -> Optional[str]:
    # 1) meta[name=book_detail]
    meta_tag = soup.select_one('meta[name="book_detail"]')
    if meta_tag and meta_tag.get("content"):
        val = (meta_tag["content"] or "").strip()
        if val.isdigit():
            return val
    # 2) regex dự phòng trên HTML thô
    for rx in (
        re.compile(r"/doc-truyen/page/(\d+)", re.I),
        re.compile(r"(?:var|let|const)?\s*(?:storyId|bookId|contentId)\s*[:=]\s*(\d+)", re.I),
        re.compile(r"page/\s*(\d+)\s*\?limit=\d+", re.I),
    ):
        m = rx.search(raw_html)
        if m:
            return m.group(1)
    return None

# ----------------- public API -----------------

def fetch_book_meta_and_chapters(
    url: str,
    delay: float = DEFAULT_DELAY,
    per_page: int = DEFAULT_LIMIT,
) -> Tuple[str, str, List[str], str, str, List[Dict], Callable[[str], str]]:
    """
    Trả về: (title, author, genres, status, cover_url, chapters, fetch_content_fn)
    """
    base_url = url.split("?")[0].rstrip("/")
    sess = _make_session()

    # WARM-UP cookie
    r0 = sess.get(base_url, timeout=DEFAULT_TIMEOUT,
                  headers={"Referer": base_url}, verify=not INSECURE_SSL)
    r0.raise_for_status()
    raw0 = r0.text
    soup = BeautifulSoup(raw0, "html.parser")

    # ---- meta ----
    title_node = soup.select_one(".book-title h1") or soup.select_one("h1") \
        or soup.select_one(".title h1") or soup.select_one(".title")
    title = _clean(title_node.get_text(" ", strip=True) if title_node else "")
    author = _pick_text_by_label(soup, ["Tác giả", "Tac gia", "Author"])
    genres_text = _pick_text_by_label(soup, ["Thể loại", "The loai", "Genre"])
    genres = [g.strip() for g in re.split(r"[,\|/]", genres_text) if g.strip()]
    status = _pick_text_by_label(soup, ["Tình trạng", "Trạng thái", "Status"])
    cover_url = _find_cover_url(soup, base_url)

    # ---- phân trang ----
    book_id = _extract_book_id_from_page(soup, raw0)
    last_label, last_index = _guess_last_label_and_last_index(soup)

    chapters: List[Dict] = []
    if book_id:
        seen = set()

        def fetch_page(ix: int) -> Optional[str]:
            api = f"https://truyen.tangthuvien.vn/doc-truyen/page/{book_id}?page={ix}&limit={per_page}&web=1"
            for tries in range(3):
                try:
                    r = sess.get(
                        api, timeout=DEFAULT_TIMEOUT, verify=not INSECURE_SSL,
                        headers={"Referer": base_url, "X-Requested-With": "XMLHttpRequest", **HEADERS},
                    )
                    if r.status_code == 200:
                        return r.text
                except Exception:
                    pass
                time.sleep(delay * (tries + 1))
            return None

        # Range trang
        if last_index is not None:
            start_ix, end_ix = 0, last_index            # chính xác theo "Trang cuối"
        elif last_label is not None:
            start_ix, end_ix = 0, max(0, last_label - 1)
        else:
            start_ix, end_ix = 0, None  # open-ended (ít chương)

        empty_streak = 0
        ix = start_ix
        while True:
            if end_ix is not None and ix > end_ix:
                break

            body = fetch_page(ix)

            # Nếu biết end_ix (đã có "Trang cuối"): KHÔNG dừng vì rỗng
            if not body:
                if end_ix is None:
                    empty_streak += 1
                    if empty_streak >= 3:
                        break
                ix += 1
                time.sleep(delay)
                continue

            items = _parse_chapter_list(body, base_url)

            # Dedupe theo URL chuẩn hoá
            new_items: List[Dict] = []
            for it in items:
                u = it["url"]
                if u in seen:
                    continue
                seen.add(u)
                new_items.append(it)

            if not new_items and end_ix is None:
                empty_streak += 1
                if empty_streak >= 3:
                    break
            else:
                empty_streak = 0

            if new_items:
                chapters.extend(new_items)
                total_pages_hint = last_index if last_index is not None else (last_label or "?")
                print(f"📄 Loaded page {ix}/{total_pages_hint} — total: {len(chapters)} (+{len(new_items)})")

            ix += 1
            time.sleep(delay)

    # ---- lấy nội dung chương ----
    def fetch_chapter_content(chap_url: str) -> str:
        rx = requests.get(chap_url, timeout=DEFAULT_TIMEOUT,
                          headers={"Referer": base_url, **HEADERS}, verify=not INSECURE_SSL)
        rx.raise_for_status()
        sp = BeautifulSoup(rx.text, "html.parser")
        candidates = sp.select(".box-chap, .box-chap p, .chapter-c, .chapter-content, #chapter-c")
        if not candidates:
            return f"<p>{html.escape(_clean(sp.get_text(' ', strip=True)))}</p>"
        best = max(candidates, key=lambda n: len(n.get_text(" ", strip=True)))
        parts = []
        # gom p/div/br
        for p in best.find_all(["p", "div", "br"]):
            t = _clean(p.get_text(" "))
            if t:
                parts.append(f"<p>{html.escape(t)}</p>")
        if not parts:
            t = _clean(best.get_text(" "))
            if t:
                parts.append(f"<p>{html.escape(t)}</p>")
        return "\n".join(parts)

    return title, author, genres, status, cover_url, chapters, fetch_chapter_content
