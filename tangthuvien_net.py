# -*- coding: utf-8 -*-
"""
Plugin cho: https://truyen.tangthuvien.vn/ và https://tangthuvien.net/
- Lấy meta: Tên truyện, Tác giả, Thể loại, Tình Trạng
- Lấy đủ danh sách chương (nhiều trang / API page)
- Tải nội dung chương (gộp nhiều block .box-chap), tự chèn <p> khi cần
- Lưu HTML + index (tên file giữ nguyên dấu)
"""

import requests, re, time, os, html, unicodedata
from typing import Optional, List, Dict
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 25
SLEEP_BETWEEN_PAGES = 0.15
SLEEP_BETWEEN_CHAPS = 0.10

def safe_filename_unicode(s: str, limit: int = 150) -> str:
    import re, unicodedata
    s = (s or "").strip()
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r'[\\/:*?"<>|\x00-\x1F]+', "—", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(" .") or "untitled"
    return s[:limit].rstrip(" .") or "untitled"

# ----- TLS adapter (fix SSLEOF với một số máy chủ) -----
class TLS12HttpAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        from urllib3.util import ssl_
        ctx = ssl_.create_urllib3_context()
        super().init_poolmanager(*args, ssl_context=ctx, **kwargs)

session = requests.Session()
session.headers.update(HEADERS)
session.mount("https://", TLS12HttpAdapter())

def _fetch_html(url: str) -> BeautifulSoup:
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "html.parser")

def _is_truyen_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return ("truyen.tangthuvien.vn" in host) or ("tangthuvien.net" in host)

def _normalize_book_url(url: str) -> str:
    # chuẩn hoá sang truyen.tangthuvien.vn nếu là net
    if "tangthuvien.net" in url:
        # giữ path sau /doc-truyen/..., đổi host
        p = urlparse(url)
        return f"https://truyen.tangthuvien.vn{p.path}"
    return url

def _extract_book_id(html_soup: BeautifulSoup) -> Optional[str]:
    # meta name="book_detail" content="38510"
    m = html_soup.select_one('meta[name="book_detail"][content]')
    return m["content"].strip() if m else None

def _meta_info(soup: BeautifulSoup) -> Dict[str, str]:
    # tiêu đề
    title = None
    tnode = soup.select_one("title")
    if tnode:
        title = tnode.get_text(strip=True)
        # xoá phần rườm rà
        title = re.sub(r"(?i)\s*-\s*Trang chủ.*$", "", title)
    # tác giả / thể loại / tình trạng
    author = ""
    status = ""
    genres = []
    # khối book-info có p.tag, p.author v.v
    info = soup.select_one("div.book-info") or soup
    a = info.select_one("p.author a")
    if a: author = a.get_text(strip=True)
    # thể loại
    for g in info.select("p.tag a[href*='/the-loai/']"):
        t = g.get_text(strip=True)
        if t and t not in genres: genres.append(t)
    # tình trạng
    st = info.find("p", class_="status")
    if st:
        status = st.get_text(" ", strip=True)
        status = re.sub(r"(?i)^\s*Tình trạng\s*:\s*", "", status)
    return {"title": title or "", "author": author or "", "genres": genres, "status": status or ""}

def _api_list_url(book_id: str, page: int, limit: int = 75) -> str:
    return f"https://truyen.tangthuvien.vn/doc-truyen/page/{book_id}?page={page}&limit={limit}&web=1"

def _collect_chapter_links(url: str) -> List[Dict[str, str]]:
    url = _normalize_book_url(url)
    soup = _fetch_html(url)
    book_id = _extract_book_id(soup)
    if not book_id:
        raise RuntimeError("Không tìm thấy book_id (meta[name='book_detail']).")

    # lấy số trang từ pagination (nếu có)
    max_page = 1
    for a in soup.select("ul.pagination a[onclick]"):
        m = re.search(r"Loading\((\d+)\)", a.get("onclick",""))
        if m:
            max_page = max(max_page, int(m.group(1)) + 1)  # Loading(0)=>trang đầu => coi như có 1
    # fallback nếu không thấy: thử tăng dần cho tới khi API trả rỗng
    links, seen = [], set()
    page = 0
    while True:
        api = _api_list_url(book_id, page)
        r = session.get(api, headers={"X-Requested-With":"XMLHttpRequest"}, timeout=TIMEOUT)
        r.raise_for_status()
        txt = r.text or ""
        # mỗi item có pattern href="/doc-truyen/<slug>/<slug-chuong>"
        found = re.findall(r'href="(/doc-truyen/[^"]+)"', txt)
        titles = re.findall(r'title="([^"]+)"', txt)
        if not found:
            if page == 0:
                # một số truyện chỉ có 1 trang, không cần API
                pass
            else:
                break
        count_this = 0
        for idx, h in enumerate(found):
            full = urljoin("https://truyen.tangthuvien.vn/", h)
            t = titles[idx] if idx < len(titles) else ""
            if full not in seen:
                seen.add(full)
                links.append({"title": t, "url": full})
                count_this += 1
        print(f"Loaded page {page} — collected {count_this} links (total: {len(links)})")
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
        # nếu max_page xác định được, dừng khi đủ
        if max_page > 1 and page >= max_page: break

    if not links:
        # fallback parse ngay trang html (list đầu)
        for a in soup.select("div.list-chapter a[href]"):
            t = a.get("title") or a.get_text(strip=True)
            href = urljoin(url, a["href"])
            if href not in seen:
                seen.add(href); links.append({"title": t, "url": href})

    return links

def getText(url: str):
    url = _normalize_book_url(url)
    soup = _fetch_html(url)
    info = _meta_info(soup)
    chapters = _collect_chapter_links(url)
    # Thứ tự tăng dần: API trả theo nhiều kiểu — đảm bảo ổn định
    def keyfun(x):
        m = re.search(r"(\d+)", x.get("title") or "")
        return int(m.group(1)) if m else 10**9
    chapters.sort(key=keyfun)
    print(f"Collected chapter links: {len(chapters)}")
    return {
        "title": info["title"],
        "author": info["author"],
        "genres": info["genres"],
        "status": info["status"],
        "chapters": chapters,
        "total_pages": None,
    }

def _clean_node(node: BeautifulSoup) -> str:
    for sel in ["script","style","noscript","iframe","form",".ads",".ad",".banner",".share",".comment"]:
        for t in node.select(sel):
            t.decompose()
    html_str = str(node)
    html_str = re.sub(r"<br\s*>", "<br/>", html_str, flags=re.I)
    html_str = re.sub(r"<p>\s*(?:&nbsp;|\u00A0|\s)*</p>", "", html_str, flags=re.I)
    return html_str

def fetch_chapter_content(url: str):
    soup = _fetch_html(url)
    # Tiêu đề
    title = None
    for sel in ["h1.title","h2.title",".chapter-title","a.chapter-title"]:
        n = soup.select_one(sel)
        if n and n.get_text(strip=True):
            title = n.get_text(strip=True); break
    if not title:
        h = soup.find(["h1","h2","h3"]); title = h.get_text(strip=True) if h else None

    # Nội dung: nhiều block .box-chap*. Gộp lại.
    container = soup.select("div[class^='box-chap']")
    html_parts = []
    if container:
        for div in container:
            cleaned = _clean_node(div)
            text_only = BeautifulSoup(cleaned, "html.parser").get_text("\n", strip=True)
            if "<p" not in cleaned.lower() and "<br" not in cleaned.lower() and len(text_only) > 100:
                # chèn <p> theo đoạn
                paras = [p.strip() for p in re.split(r"\n{2,}", text_only) if p.strip()]
                cleaned = "".join(f"<p>{html.escape(p)}</p>" for p in paras)
            html_parts.append(cleaned)
    else:
        # fallback
        main = soup.select_one("#chapter-content, #chapter-c, .chapter-content, #content, .reading .content")
        if main:
            html_parts.append(_clean_node(main))

    content_html = "\n".join(html_parts) if html_parts else "<p>(Không có nội dung)</p>"
    return {"title": title, "content_html": content_html, "url": url}

HTML_TEMPLATE = """<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{doc_title}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.7;max-width:820px;margin:2rem auto;padding:0 1rem;background:#f7f7f9;color:#222}}
h1{{font-size:1.6rem;margin:0 0 1rem}}
.meta{{color:#666;font-size:.9rem;margin-bottom:1rem}}
img{{max-width:100%;height:auto}}
p{{margin:.6rem 0}}
article{{background:#fff;border-radius:12px;padding:1rem 1.2rem;box-shadow:0 1px 10px rgba(0,0,0,.06)}}
</style></head>
<body>
<h1>{chapter_title}</h1>
<div class="meta">{book_title} · <a href="{src}">Nguồn</a></div>
<article>
{content}
</article>
</body></html>"""

def save_chapter_html(book_title: str, chapter_idx: int, chap: dict, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{chapter_idx:04d} - {safe_filename_unicode(chap.get('title') or f'Chương {chapter_idx}')}.html"
    path  = os.path.join(out_dir, fname)
    html_out = HTML_TEMPLATE.format(
        doc_title     = f"{book_title} - {chap.get('title') or f'Chương {chapter_idx}'}",
        chapter_title = html.escape(chap.get('title') or f'Chương {chapter_idx}'),
        book_title    = html.escape(book_title or "Truyện"),
        src           = chap.get("url") or "",
        content       = chap.get("content_html") or "<p>(Không có nội dung)</p>",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_out)
    return path

def save_all_chapters_to_html(book_title: str, chapters: list, out_dir: str,
                              start: int = 1, end: Optional[int] = None):
    n = len(chapters)
    if end is None or end > n: end = n
    saved = []
    for i in range(start, end + 1):
        info = chapters[i-1]
        try:
            chap = fetch_chapter_content(info["url"])
            if not chap.get("title"): chap["title"] = info.get("title")
            p = save_chapter_html(book_title, i, chap, out_dir)
            print(f"[{i:04d}/{n}] Saved HTML: {p}", flush=True)
            saved.append(p)
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"[{i:04d}/{n}] ERROR {info.get('url')}: {e}", flush=True)
    return saved

def save_index_html(book_title: str, author: str, genres: list, status: str,
                    chapters: list, out_dir: str):
    rows = []
    for i, c in enumerate(chapters, 1):
        name = safe_filename_unicode(c.get("title") or f"Chương {i}")
        rows.append(f'<li><a href="{i:04d} - {name}.html">{html.escape(c.get("title") or f"Chương {i}")}</a></li>')
    cont = f"""<!doctype html><html lang="vi"><head>
<meta charset="utf-8"/><title>{html.escape(book_title)} - Index</title>
<style>body{{font-family:system-ui;max-width:900px;margin:2rem auto;line-height:1.7}}</style>
</head><body>
<h1>{html.escape(book_title)}</h1>
<p><b>Tác giả:</b> {html.escape(author or "—")} &nbsp;|&nbsp;
<b>Thể loại:</b> {html.escape(", ".join(genres) or "—")} &nbsp;|&nbsp;
<b>Tình trạng:</b> {html.escape(status or "—")}</p>
<hr/>
<ol>
{''.join(rows)}
</ol>
</body></html>"""
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(cont)
