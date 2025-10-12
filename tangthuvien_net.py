# -*- coding: utf-8 -*-
# TangThuVien extractor (tangthuvien.net / truyen.tangthuvien.vn)
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, urljoin
import requests, re, os, html, unicodedata, ssl
from typing import List, Dict, Optional, Tuple
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36")
}
TIMEOUT = 30
INSECURE_SSL = False
MIRRORS = ["tangthuvien.net", "truyen.tangthuvien.vn"]

# -------- TLS 1.2 Adapter --------
class TLS12HttpAdapter(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = create_urllib3_context()
        try: ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except Exception:
            ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)
    def proxy_manager_for(self, *a, **kw):
        ctx = create_urllib3_context()
        try: ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except Exception:
            ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        kw["ssl_context"] = ctx
        return super().proxy_manager_for(*a, **kw)

def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, connect=3, read=3, backoff_factor=0.6,
                  status_forcelist=[429,500,502,503,504],
                  allowed_methods=["GET","HEAD"], raise_on_status=False)
    adapter = TLS12HttpAdapter(max_retries=retry)
    s.mount("https://", adapter); s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s

# ====== UTIL ======
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
            r = sess.get(test_url, timeout=TIMEOUT, verify=not INSECURE_SSL)
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except requests.exceptions.RequestException as e:
            last_err = e; continue
    if not INSECURE_SSL and last_err:
        try:
            r = sess.get(url, timeout=TIMEOUT, verify=False); r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except Exception: raise last_err
    if last_err: raise last_err
    raise RuntimeError("Không tải được trang")

def _pick_title(soup: BeautifulSoup) -> Optional[str]:
    for sel in ["h1",".title h1",".book-title h1",".book-title",".title"]:
        n = soup.select_one(sel)
        if n and _clean_text(n.get_text()): return _clean_text(n.get_text())
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"): return _clean_text(og["content"])
    t = soup.find("title")
    return _clean_text(t.get_text()) if t else None

# ====== META ======
LABEL_KEYS = {
    "author": ["tác giả","tac gia","author"],
    "genres": ["thể loại","the loai","genre"],
    "status": ["tình trạng","trạng thái","status"],
}

def _gather_label_value_pairs(soup: BeautifulSoup) -> List[Dict[str, str]]:
    pairs=[]
    for block in soup.select(".book-info p.tag, .book-information, .book-meta, .truyen-info"):
        txt = _clean_text(block.get_text(" ", strip=True))
        if not txt: continue
        # Thô nhưng hiệu quả cho TTV: 'Tác giả ... Thể loại ...'
        for m in re.finditer(r"(?P<label>Tác giả|Tình trạng|Thể loại)\s*:?\s*(?P<val>[^|]+)", txt, flags=re.I):
            pairs.append({"label": m.group("label"), "value": m.group("val")})
    return pairs

def _match_value(pairs: List[Dict[str, str]], key: str) -> Optional[str]:
    kws = LABEL_KEYS[key]
    for p in pairs:
        lab=_clean_text(p.get("label")); val=_clean_text(p.get("value"))
        if lab and val and any(k in lab.lower() for k in kws):
            return val
    return None

def _extract_from_bookinfo(soup: BeautifulSoup):
    author=None; status=None; genres=[]
    tag = soup.select_one(".book-info p.tag")
    if tag:
        for a in tag.find_all("a", href=True):
            if "tac-gia" in a["href"].lower() or "author" in a["href"].lower():
                author = _clean_text(a.get_text())
        sp = tag.find("span")
        if sp: status = _clean_text(sp.get_text())
        for a in tag.find_all("a", href=True):
            if "/the-loai/" in a["href"].lower():
                g = _clean_text(a.get_text()); 
                if g and g not in genres: genres.append(g)
    return author or None, status or None, genres

def _extract_genres_as_list(value: Optional[str], soup: BeautifulSoup) -> List[str]:
    if not value: return []
    parts = [ _clean_text(x) for x in re.split(r"[,/|·•;–\-–]+", value) if _clean_text(x)]
    # unique
    out=[]; seen=set()
    for p in parts:
        if p not in seen: out.append(p); seen.add(p)
    return out

# ====== CHAPTER LIST ======
_CHAP_RE = re.compile(r"(?:chuong[-\s]*|chapter[-\s]*)(\d+)", flags=re.I)
def _parse_chapter_no(text: str, href: str) -> Optional[int]:
    m = re.search(r"chương\s*(\d+)", text, flags=re.I)
    if m: return int(m.group(1))
    m = _CHAP_RE.search(href)
    return int(m.group(1)) if m else None

def _extract_chapters(soup: BeautifulSoup, base_url: str) -> Tuple[List[Dict], Dict[str, Optional[int]]]:
    # id sách
    book_id=None
    meta_tag = soup.select_one('meta[name="book_detail"]')
    if meta_tag and meta_tag.get("content"):
        book_id = meta_tag["content"].strip()

    def parse_chapter_page(html_text: str) -> List[Dict]:
        s = BeautifulSoup(html_text, "html.parser")
        anchors = s.select('.catalog-content-wrap a[href*="/chuong"]') or s.select('a[href*="/chuong"]')
        out=[]
        for a in anchors:
            t = _clean_text(a.get_text()); href=a.get("href","")
            if not href: continue
            url = urljoin(base_url, href)
            out.append({"title": t, "url": url, "chap_no": _parse_chapter_no(t, href)})
        return out

    chapters = parse_chapter_page(str(soup))
    pages_crawled = 1

    def _guess_last_page_from_dom(dom: BeautifulSoup) -> Optional[int]:
        nums=[]
        for a in dom.select("ul.pagination a"):
            oc=a.get("onclick","")
            m=re.search(r"Loading\((\d+)\)", oc)
            if m: nums.append(int(m.group(1)))
            t=_clean_text(a.get_text())
            if t.isdigit(): nums.append(int(t))
        return max(nums) if nums else None

    last_page = _guess_last_page_from_dom(soup)

    if book_id:
        sess = _make_session()
        page = 1
        hard_stop = last_page is not None
        max_page = (last_page - 1) if hard_stop else 10**9
        prev_total = len(chapters)
        while page <= max_page:
            api = f"https://truyen.tangthuvien.vn/doc-truyen/page/{book_id}?page={page}&limit=75&web=1"
            try:
                r = sess.get(api, timeout=15,
                             headers={"Referer": base_url,"X-Requested-With":"XMLHttpRequest"},
                             verify=not INSECURE_SSL)
                if r.status_code != 200 or "/chuong" not in r.text.lower():
                    break
                new_items = parse_chapter_page(r.text)
                if not new_items: break
                chapters.extend(new_items)
                pages_crawled += 1
                print(f"📄 Loaded page {page}/{(last_page-1) if hard_stop else '?'} — total: {len(chapters)}")
                if not hard_stop:
                    if len(chapters)==prev_total: break
                    prev_total=len(chapters)
                    frag=BeautifulSoup(r.text,"html.parser")
                    lp2=_guess_last_page_from_dom(frag)
                    if lp2 and lp2!=last_page:
                        last_page=lp2; max_page=last_page-1
                page += 1
            except Exception as e:
                print(f"⚠️ Stop at page {page}: {e}"); break

    # unique + sort
    seen=set(); unique=[]
    for c in chapters:
        k=(c["url"], c["title"])
        if k in seen: continue
        seen.add(k); unique.append(c)
    unique.sort(key=lambda x: (999999 if x["chap_no"] is None else x["chap_no"], x["title"]))
    print(f"🔗 Collected chapter links: {len(unique)} (pages crawled: {pages_crawled}, last_page label: {last_page or '?'})")
    return unique, {"pages_crawled": pages_crawled, "guessed_last_page": last_page}

# ====== CHAPTER CONTENT ======
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
_REMOVE_INSIDE = [
    ".box-adv",".ads",".adsbox",".advertisement",".banner",
    ".share",".like",".social",".fb","ins","iframe",
    ".chapter-nav",".chapter-control",".tool-box",".control-box",
    ".left-control",".right-control",
    "#list-comment",".list-comment",".comment","#comments",
    "script","noscript","style",
]

def _extract_chapter_title(soup: BeautifulSoup) -> str:
    for sel in [".chapter-c-content h5 a",".chapter-c-content h5",
                ".chapter-c-content h4 a",".chapter-c-content h4",
                ".book-content-wrap h1",".book-content-wrap h2","h1","h2"]:
        n=soup.select_one(sel)
        if n and _clean_text(n.get_text()):
            return _clean_text(n.get_text())
    t=soup.find("title")
    return re.split(r"\s+[-–]\s+", _clean_text(t.get_text()), 1)[0] if t else "Chương ?"

def _extract_content_boxes(soup: BeautifulSoup) -> List[BeautifulSoup]:
    boxes=[]
    for sel in _CONTENT_SELECTORS:
        found=soup.select(sel)
        if found: boxes.extend(found); break
    cleaned=[]
    for b in boxes:
        for rm in _REMOVE_INSIDE:
            for bad in b.select(rm): bad.decompose()
        if _clean_text(b.get_text()): cleaned.append(b)
    return cleaned

def _box_has_structured_breaks(box: BeautifulSoup) -> bool:
    if box.find("p") or box.find("br"): return True
    if box.find(["blockquote","li"]): return True
    children_divs = box.find_all("div", recursive=False)
    return len(children_divs) >= 2

def _render_box_as_html(box: BeautifulSoup) -> str:
    if _box_has_structured_breaks(box):
        return str(box)
    raw = box.get_text("\n", strip=True)
    paras = [p.strip() for p in re.split(r"\n\s*\n+", raw) if p.strip()]
    parts = [f"<p>{html.escape(p)}</p>" for p in paras]
    if not parts:
        sentences = re.split(r"(?<=[\.!\?…])\s+", raw)
        buf, chunk, chunks = [], 0, []
        for s in sentences:
            if not s.strip(): continue
            buf.append(s.strip()); chunk += 1
            if chunk >= 3:
                chunks.append(" ".join(buf)); buf=[]; chunk=0
        if buf: chunks.append(" ".join(buf))
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
    return {"title": title, "url": url, "content_html": content_html, "text": text}

# ====== PUBLIC API ======
def getText(url: str) -> Dict:
    soup = _fetch_html(url)
    title = _pick_title(soup) or "Truyện"
    author, status, genres = _extract_from_bookinfo(soup)
    if not (author and status and genres):
        pairs=_gather_label_value_pairs(soup)
        if not author: author=_match_value(pairs,"author")
        if not status: status=_match_value(pairs,"status")
        if not genres:
            raw=_match_value(pairs,"genres")
            genres=_extract_genres_as_list(raw, soup)
    # tổng số chương theo nhãn
    total_chapters=None
    lab=soup.select_one('#j-bookCatalogPage')
    if lab:
        m=re.search(r"\((\d+)\s*chương", _clean_text(lab.get_text()), flags=re.I)
        if m: total_chapters=int(m.group(1))
    chapters, stats = _extract_chapters(soup, url)
    if total_chapters is None and chapters: total_chapters=len(chapters)
    return {
        "title": title, "author": author or "—", "genres": genres or [],
        "status": status or "—", "total_chapters": total_chapters,
        "chapters": chapters,
        "chapter_link_count": len(chapters),
        "pages_crawled": stats.get("pages_crawled",1),
        "guessed_last_page": stats.get("guessed_last_page"),
    }

# ====== SAVE HTML & INDEX ======
HTML_TEMPLATE = """<!doctype html>
<html lang="vi">
<meta charset="utf-8">
<title>{doc_title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;line-height:1.7;
      padding:24px;max-width:860px;margin:0 auto;background:#f7f7f9;color:#222}}
h1{{font-size:1.6rem;margin:0 0 .8rem}}
.meta{{color:#666;font-size:.9rem;margin-bottom:1rem}}
.chapter{{margin-top:1rem}}
.chapter p{{margin:.45rem 0}}
img{{max-width:100%;height:auto}}
a{{color:#0b73d9;text-decoration:none}}
a:hover{{text-decoration:underline}}
</style>
<h1>{chapter_title}</h1>
<div class="meta">{book_title} · <a href="{src}">Nguồn</a></div>
<div class="chapter">
{content}
</div>
"""

def save_chapter_html(book_title: str, chapter_idx: int, chap: dict, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", chap.get("title") or f"Chuong {chapter_idx}")
    fname = f"{chapter_idx:04d} - {safe}.html"
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
    import time
    n=len(chapters); end = n if end is None or end>n else end
    for i in range(start, end+1):
        info=chapters[i-1]
        try:
            c=get_chapter(info["url"])
            if not c.get("title"): c["title"]=info.get("title")
            p=save_chapter_html(book_title, i, c, out_dir)
            print(f"[{i:04d}/{n}] Saved HTML: {p}", flush=True)
            time.sleep(0.1)
        except Exception as e:
            print(f"[{i:04d}/{n}] ERROR {info.get('url')}: {e}", flush=True)

def save_index_html(out_dir: str, title: str, author: str, genres: list, status: str, chapters: list):
    os.makedirs(out_dir, exist_ok=True)
    items=[]
    for i, c in enumerate(chapters, 1):
        items.append(f'<li><a href="{i:04d} - {html.escape(c.get("title") or f"Chuong {i}")}.html">{html.escape(c.get("title") or f"Chương {i}")}</a></li>')
    genres_txt=", ".join(genres or [])
    html_doc=f"""<!doctype html>
<html lang="vi">
<meta charset="utf-8">
<title>{html.escape(title)} — Mục lục</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;line-height:1.7;
      padding:24px;max-width:860px;margin:0 auto;background:#f7f7f9;color:#222}}
h1{{font-size:1.8rem;margin:0 0 .6rem}}
.meta{{color:#555;margin:0 0 1rem}}
ol{{padding-left:1.25rem}}
.badge{{display:inline-block;background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;padding:.1rem .5rem;margin-right:.35rem}}
</style>
<h1>{html.escape(title)}</h1>
<div class="meta">
  <span class="badge">Tác giả: {html.escape(author or "—")}</span>
  <span class="badge">Thể loại: {html.escape(genres_txt or "—")}</span>
  <span class="badge">Tình trạng: {html.escape(status or "—")}</span>
</div>
<ol>
{''.join(items)}
</ol>
</html>"""
    with open(os.path.join(out_dir,"index.html"),"w",encoding="utf-8") as f:
        f.write(html_doc)
