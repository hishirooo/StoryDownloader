# -*- coding: utf-8 -*-
"""
    @Author: Hishio - Cá mụp cắn cáp
    @github: https://github.com/hishirooo
    @date: 18-10-2025
    @version: 1.0
    tangthuvien_net.py — Trình tải chương & tạo EPUB cho tangthuvien

    CHẾ ĐỘ:
    1) Tải HTML
    2) Tải TXT
    3) Tải HTML + Tạo EPUB (đọc lại HTML đã tải)
    4) Tải TXT  + Tạo EPUB (TXT -> bọc XHTML tối giản -> EPUB)
    5) Tải HTML + TXT + Tạo EPUB (EPUB dùng HTML đã tải)

    COVER:
    - Sau khi nhập URL, bạn nhập đường dẫn cover (file local hoặc URL ảnh).
    - Bỏ trống sẽ tự lấy cover từ DOM: div.book-img img[src]
    - Nếu ảnh không phải JPG/PNG → convert 1 lần sang JPEG (cần Pillow).

    LOG:
    - Thư mục đầu ra: ./Output/<Tên Truyện> (không dấu hoặc có dấu tùy cấu hình)
    - EPUB sẽ lưu ở: ./Output/<TenTruyen>.epub (không nằm trong thư mục truyện)
    - Khi tải:   Saved : 0001.xhtml - <Tiêu đề chương>
    - Khi tạo:   Readfile : 0001.xhtml from Output/<Tên Truyện>


"""

from typing import Optional, List, Dict, Tuple
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, urljoin
import requests, ssl, urllib3, re, json, html, os, unicodedata, zipfile, io, datetime, shutil

from requests.adapters import HTTPAdapter
from epub_metadata import subject_xml
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

# ============ LOG đơn giản ============

class Logger:
    def __init__(self, log_file: str):
        self.log_file = log_file
        ensure_dir(os.path.dirname(log_file))
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write("=== START: {} ===\n".format(datetime.datetime.now().isoformat(timespec="seconds")))
    def log(self, msg: str) -> None:
        print(msg)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

# ====================== THÔNG TIN TRUYỆN & COVER ======================

def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    """Lấy title/author/genre/status theo layout TTV (có fallback)."""
    info = {"title": "", "author": "", "genre": "", "status": ""}
    try:
        book_info = soup.find('div', class_='book-info')
        if book_info:
            h1 = book_info.find('h1')
            info["title"] = _text(h1)
            tag = book_info.find('p', class_='tag') or book_info.find('p', class_='tags')
            if tag:
                a_blue = tag.find('a', class_='blue')
                s_blue = tag.find('span', class_='blue')
                a_red  = tag.find('a', class_='red')
                if not a_blue:
                    blues = tag.find_all('a', class_='blue')
                    if blues: a_blue = blues[0]
                if not a_red:
                    reds = tag.find_all('a', class_='red')
                    if reds: a_red = reds[0]
                info["author"] = _text(a_blue)
                info["status"] = _text(s_blue)
                info["genre"]  = _text(a_red)
        if not info["title"] and soup.title:
            info["title"] = _text(soup.title)
    except Exception:
        pass
    return info

def _get_book_id_from_meta(soup: BeautifulSoup) -> Optional[str]:
    """Đọc <meta name="book_detail" ...> để lấy story_id (hỗ trợ số/JSON)."""
    meta = soup.find('meta', attrs={'name': 'book_detail'})
    if not meta: return None
    content = (meta.get('content') or '').strip()
    if not content: return None
    if content.isdigit(): return content
    if content.startswith('{') and content.endswith('}'):
        try:
            data = json.loads(content)
            for k in ('story_id','id','book_id'):
                v = str(data.get(k,'')).strip()
                if v.isdigit(): return v
        except Exception:
            pass
    m = re.search(r'([0-9]{3,})', content)
    return m.group(1) if m else None

def _find_cover_url_from_page(StoryUrl: str) -> Optional[str]:
    """Lấy URL ảnh bìa từ DOM: div.book-img img[src] (fallback og:image)."""
    soup = _fetch_html(StoryUrl)
    img = soup.select_one("div.book-img img")
    if img and img.get("src"):
        return urljoin(BASE + "/", _canonicalize_domain(img["src"].strip()))
    meta = soup.find("meta", attrs={"property":"og:image"}) or soup.find("meta", attrs={"name":"og:image"})
    if meta and meta.get("content"):
        return _canonicalize_domain(meta["content"].strip())
    return None

# ====================== DANH SÁCH CHƯƠNG (API) ======================

def _get_list_chapters(StoryUrl: str) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    soup = _fetch_html(StoryUrl)
    story_id = _get_book_id_from_meta(soup)
    if not story_id:
        print("Không tìm thấy story_id")
        return chapters
    try:
        resp = _session.get(f"{BASE}/story/chapters", params={"story_id": story_id}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print("Lỗi lấy danh sách chương:", e)
        return chapters
    s = BeautifulSoup(resp.text, "html.parser")
    for a in s.find_all("a"):
        title = (a.get("title") or a.get_text(strip=True) or "").strip()
        href  = (a.get("href") or "").strip()
        if not title or not href:
            continue
        chapters.append({"title": title, "link": urljoin(BASE + "/", href)})
    return chapters

def getinfo(StoryUrl: str) -> dict:
    """In thông tin truyện + trả dict."""
    soup = _fetch_html(StoryUrl)
    info = _get_book_info(soup)
    story_id = _get_book_id_from_meta(soup)
    chapters = _get_list_chapters(StoryUrl)
    print("-----------------THÔNG TIN TRUYỆN:-------------------")
    print(f"Title : {info.get('title','')}")
    print(f"Author: {info.get('author','')}")
    print(f"Genre : {info.get('genre','')}")
    print(f"Status: {info.get('status','')}")
    print(f"Story ID: {story_id}")
    print(f"Saved {len(chapters)} link chapter from API.")
    return {"info": info, "story_id": story_id, "chapters": chapters}

# ====================== NỘI DUNG CHƯƠNG (LÀM SẠCH & TÁCH ĐOẠN TỪ BOX-CHAP) ======================

_TITLE_SELECTORS = [
    "h1.chapter-title","h2.chapter-title","div.chapter h2",".chapter h2",
    ".chapter .title","h1.title","h2.title","h1","h2",
    ".chapter-name",".chap-name",".entry-title",".book-chapter h1",".book-chapter h2",
]

# Một số câu “rác” hay xuất hiện
_JUNK_PATTERNS = [
    r"(?i)\btặng phiếu\b",
    r"(?i)b\s*ả\s*n tác phẩm.*sửa sang.*truyền lên",   # dòng chú thích
    r"(?i)\bbản quyền\b|\bđăng tại\b|\bnguồn\b",       # banner/credit
    r"^-{2,}$",                                        # gạch phân cách
]

def _is_junk_line(s: str) -> bool:
    s = (s or "").strip()
    if not s: 
        return True
    for p in _JUNK_PATTERNS:
        if re.search(p, s):
            return True
    return False

def _get_chapter_title_basic(soup: BeautifulSoup) -> str:
    """Tiêu đề chương tối giản: ưu tiên h1/h2 rồi fallback og:title/title trang."""
    for sel in _TITLE_SELECTORS:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t
    og = soup.find("meta", attrs={"property":"og:title"}) or soup.find("meta", attrs={"name":"og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title:
        return soup.title.get_text(" ", strip=True)
    return ""

def _split_sentences_vi(text: str) -> list[str]:
    """
    Tách câu cho TV/TV-Hoa:
    - Dấu kết câu: . ! ? … ; và fullwidth Trung: 。 ！ ？ ；
    - Cho phép có ngoặc kép đóng ” đứng sau.
    - Trước khi tách: chuẩn hoá '...'/'. . .' -> '…', và gọn ngoặc 《 》.
    """
    text = (text or "").replace("\u200b", " ").replace("\ufeff", " ").replace("\xa0", " ").replace("\u3000", " ")
    text = _normalize_ellipsis_and_brackets(text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = re.split(r'(?<=[\.\!\?\…;\u3002\uFF01\uFF1F\uFF1B])”?\s+', text)
    # lọc rỗng
    parts = [p.strip() for p in parts if p and p.strip()]
    return parts

def _group_sentences(sents: list[str], max_len: int = 300) -> list[str]:
    """Gộp nhiều câu thành 1 đoạn ~300 ký tự để đọc vừa mắt."""
    paras, buf, acc = [], [], 0
    for s in sents:
        if not s or _is_junk_line(s):
            continue
        buf.append(s)
        acc += len(s)
        if acc >= max_len or s.endswith((".”","!”","?”",".","!","?","…")):
            para = " ".join(buf).strip()
            if para and not _is_junk_line(para):
                paras.append(para)
            buf, acc = [], 0
    if buf:
        para = " ".join(buf).strip()
        if para and not _is_junk_line(para):
            paras.append(para)
    return paras

def _paras_from_box(box: BeautifulSoup) -> list[str]:
    """
    Rút đoạn từ 1 <div class="box-chap ...">:
    - Nếu có <p> -> lấy từng p
    - Nếu có <br> -> đổi <br> thành xuống dòng -> tách theo dòng trống
    - Nếu chỉ còn text liền mạch -> tách theo câu rồi gộp ~300 ký tự
    """
    # 1) Ưu tiên <p>
    ps = box.find_all("p")
    if ps:
        out = []
        for p in ps:
            t = p.get_text(" ", strip=True)
            t = t.replace("\u200b", " ").replace("\ufeff", " ").replace("\xa0", " ").replace("\u3000", " ").strip()
            if t and not _is_junk_line(t):
                out.append(t)
        if out:
            return out

    # 2) Xử lý <br> / đóng thẻ thành newline
    frag = str(box)
    frag = re.sub(r"(?i)<br\s*/?>", "\n", frag)
    frag = re.sub(r"(?i)</p>|</div>|</li>|</h\d>|</section>|</article>", "\n\n", frag)
    txt = BeautifulSoup(frag, "html.parser").get_text("\n", strip=True)
    txt = txt.replace("\u200b", " ").replace("\ufeff", " ").replace("\xa0", " ").replace("\u3000", " ").strip()

    if "\n" in txt:
        # Tách theo dòng trống
        out = []
        for para in re.split(r"\n{2,}", txt):
            para = re.sub(r"[ \t]*\n[ \t]*", " ", para.strip())
            if para and not _is_junk_line(para):
                out.append(para)
        if out:
            return out

    # 3) Fallback: tách theo câu & gộp
    sents = _split_sentences_vi(txt)

    # Ghép dấu rời rạc ('.', '…', '》.', v.v.) vào câu trước thay vì tạo đoạn riêng
    merged = []
    for s in sents:
        if not s:
            continue
        # câu chỉ toàn dấu (., !, ?, …, ngoặc, dấu trích dẫn) -> ghép vào trước
        if re.fullmatch(r'[\.!\?;…“”"\'《》、，、:,：；\-\–\—]+', s):
            if merged:
                merged[-1] = (merged[-1] + s).strip()
            else:
                merged.append(s)  # trường hợp hiếm: không có gì trước
            continue
        merged.append(s)

    return _group_sentences(merged, max_len=300)

def _normalize_ellipsis_and_brackets(text: str) -> str:
    """
    - Chuẩn hoá dấu chấm lửng: '. . .' / '..' / '……' -> '…'
    - Xoá khoảng trắng quanh 《 》, và giữ dấu câu dính với 》 (tránh tạo '》' hoặc '》.' thành 1 dòng riêng)
    """
    if not text:
        return ""
    # '. . .' (có/không có khoảng trắng) hoặc nhiều hơn -> '…'
    text = re.sub(r"(?:\.\s*){3,}", "…", text)
    # '......' hoặc '……' -> '…'
    text = re.sub(r"[\.]{4,}", "…", text)
    text = re.sub(r"…{2,}", "…", text)
    # bỏ khoảng trắng quanh 《 》
    text = re.sub(r"\s*([《》])\s*", r"\1", text)
    # giữ dấu câu dính với 》
    text = re.sub(r"》\s*([\.!\?;…])", r"》\1", text)
    return text

def _clean_text_basic(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u200b"," ").replace("\ufeff"," ").replace("\xa0"," ").replace("\u3000"," ")
    s = re.sub(r"(?:\.\s*){3,}", "…", s)     # ". . ." / ".  .  ." -> "…"
    s = re.sub(r"[\.]{4,}", "…", s)          # "......" -> "…"
    s = re.sub(r"…{2,}", "…", s)             # "……" -> "…"
    s = re.sub(r"\s*([《》])\s*", r"\1", s)   # gọn ngoặc 《 》
    s = re.sub(r"》\s*([\.!\?;…])", r"》\1", s)  # dính dấu vào 》
    return re.sub(r"\s+", " ", s).strip()

def _split_box_to_paragraphs(box: BeautifulSoup) -> list[str]:
    """
    Từ <div class="box-chap ..."> -> list paragraph:
    - Giữ newline thật trong box (get_text với separator="\n")
    - Tách theo dòng trống; nếu không có, tách theo từng dòng
    - Không tạo đoạn chỉ gồm dấu câu: ghép vào đoạn trước
    """
    # Nếu trong box đã có <p>, ưu tiên lấy từng p
    ps = box.find_all("p")
    if ps:
        out = []
        for p in ps:
            t = _clean_text_basic(p.get_text(" ", strip=True))
            if t:
                out.append(t)
        if out:
            return out

    # Giữ nguyên newline trong box
    raw = box.get_text("\n", strip=True)
    raw = raw.replace("\r\n", "\n")
    # Gom khoảng trắng quanh newline
    raw = re.sub(r"[ \t]*\n[ \t]*", "\n", raw)

    # 1) Tách theo dòng trống (2+ newline)
    parts = [p.strip() for p in re.split(r"\n{2,}", raw) if p.strip()]

    # 2) Nếu vẫn chỉ có 1 khối dài -> tách theo từng dòng
    if len(parts) <= 1:
        parts = [ln.strip() for ln in raw.split("\n") if ln.strip()]

    # 3) Làm sạch + ghép các dòng chỉ có dấu vào đoạn trước
    paras, buf = [], []
    for p in parts:
        t = _clean_text_basic(p)
        if not t:
            continue
        # nếu chỉ toàn dấu câu thì ghép vào đoạn trước
        if re.fullmatch(r'[\.!\?;…，,、:：”"\'》）\)\]]+', t):
            if paras:
                paras[-1] = (paras[-1] + t).strip()
            else:
                buf.append(t)
            continue
        paras.append(t)

    # Trường hợp hiếm còn dư trong buf
    if buf:
        if paras:
            paras[-1] = (paras[-1] + " " + " ".join(buf)).strip()
        else:
            paras = [" ".join(buf)]

    return paras

def extract_chapter_content(chapter_url: str) -> dict:
    """
    Lấy nội dung từ tất cả <div class="box-chap"> (kể cả hidden), giữ newline,
    tách theo dòng/box -> <p>. Bỏ qua box-adv.
    """
    soup = _fetch_html(chapter_url)

    # Lấy các box-chap theo thứ tự DOM, bỏ quảng cáo
    boxes = soup.select("div.box-chap:not([class*='box-adv'])")
    boxes = [b for b in boxes if b.get_text(strip=True)]

    paragraphs: List[str] = []
    for b in boxes:
        paragraphs.extend(_split_box_to_paragraphs(b))

    # Fallback hiếm khi không có box-chap
    if not paragraphs:
        cont = soup.select_one("#chapter-c-content, .chapter-c-content, #chapter-content, .chapter-content") or soup
        paragraphs = _split_box_to_paragraphs(cont)

    content_html = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs) if paragraphs \
                   else "<p>(Không tìm thấy nội dung chương)</p>"

    title = _get_chapter_title_basic(soup)
    return {"title": title, "content_html": content_html}

# ====================== LƯU FILE (HTML/TXT) ======================

def make_book_dir(book_title: str) -> str:
    """Tạo thư mục Output/<Tên Truyện> theo cấu hình có/không dấu."""
    folder_name = slugify_filename(book_title, allow_unicode=not USE_NO_DIACRITICS_FOLDER)
    out_dir = os.path.join(OUTPUT_ROOT, folder_name)
    ensure_dir(out_dir)
    return out_dir

def xhtml_wrap(title: str, body_html: str) -> str:
    """Khung XHTML tối giản (phục vụ EPUB)."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="vi">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <meta name="generator" content="StoryDownloader"/>
  <style>body{{font-family:serif;line-height:1.6}}h1{{font-size:1.4em}}p{{margin:0 0 .8em 0}}</style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  {body_html}
</body>
</html>
"""

def save_html(out_dir: str, idx: int, width: int, chapter_title: str, content_html: str, logger: Logger) -> str:
    '''
    Lưu nội dung chương dưới dạng HTML/XHTML.
    Trả về đường dẫn file đã lưu.
    index: số chương (dùng để đặt tên file)
    width: số chữ số trong tên file (ví dụ 4 -> 0001, 0002, ...)
    chapter_title: tên chương
    content_html: nội dung chương dưới dạng HTML
    logger: đối tượng Logger để ghi log
    '''
    ext = "xhtml" if SAVE_AS_XHTML else "html"
    fname = f"{idx:0{width}d}.{ext}"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(xhtml_wrap(chapter_title, content_html))
    logger.log(f"[{idx:04d}] Saved : {fname} - {chapter_title}")
    return fpath

def save_txt(out_dir: str, idx: int, width: int, chapter_title: str, content_html: str, logger: Logger) -> str:
    '''
    Lưu nội dung chương dưới dạng txt.
    Trả về đường dẫn file telah lưu.
    '''
    fname = f"{idx:0{width}d}.txt"
    fpath = os.path.join(out_dir, fname)
    soup = BeautifulSoup(content_html, "html.parser")
    text = soup.get_text("\n", strip=True)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(chapter_title + "\n\n" + text + "\n")
    logger.log(f"[{idx:04d}] Saved : {fname} - {chapter_title}")
    return fpath

# ====================== COVER: LẤY, CHUẨN HOÁ, NHÚNG ======================

def _standardize_ext_from_mime(mime: str) -> str:
    '''
    Chuyển đổi mime type về dạng extension.
    Ví dụ:
    - image/png → .png
    '''
    
    mime = (mime or "").split(";")[0].strip().lower()
    if mime == "image/png": return ".png"
    if mime == "image/jpeg": return ".jpg"
    if mime == "image/gif": return ".gif"
    if mime == "image/webp": return ".webp"
    return ""

def _guess_ext_from_url(u: str) -> str:
    '''
    Chuyển đổi URL về dạng extension.
    '''
    
    
    u = u.split("?")[0].split("#")[0].lower()
    for ext in (".jpg",".jpeg",".png",".gif",".webp"):
        if u.endswith(ext): return ext
    return ""

def convert_cover_to_epub_safe(in_path: str, out_dir: str, logger: Logger) -> str:
    """
    Convert sang JPEG khi KHÔNG phải jpg/png.
    - Ảnh có alpha → flatten nền trắng
    - Cần Pillow
    """
    ext = os.path.splitext(in_path)[1].lower()
    if ext in [".jpg", ".jpeg", ".png"]:
        logger.log("Cover : format already OK, skip convert.")
        if ext in [".jpeg", ".jpe"]:
            dst = os.path.join(out_dir, "cover.jpg")
            shutil.copyfile(in_path, dst)
            return dst
        return in_path

    try:
        from PIL import Image
    except Exception:
        logger.log("[WARN] Pillow không có, và cover không phải jpg/png — EPUB có thể không hiển thị trên thiết bị cũ.")
        return in_path

    try:
        img = Image.open(in_path)
        if img.mode in ("RGBA", "LA"):
            from PIL import Image as _Image
            bg = _Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            rgb = bg
        else:
            rgb = img.convert("RGB")
        out_path = os.path.join(out_dir, "cover.jpg")
        rgb.save(out_path, format="JPEG", quality=92, optimize=True)
        logger.log(f"Cover : converted -> {out_path}")
        return out_path
    except Exception as e:
        logger.log(f"[WARN] Convert cover thất bại ({e}), dùng file gốc.")
        return in_path

def prepare_cover(StoryUrl: str, user_cover: str, out_dir: str, logger: Logger) -> Optional[str]:
    """
    Trả về đường dẫn file cover trong out_dir.
    - JPG/PNG → lưu trực tiếp cover.jpg|png (không convert)
    - Khác (webp/gif/...) → lưu tạm cover_raw.ext rồi convert 1 lần → cover.jpg
    - Nếu server thiếu Content-Type → fallback theo đuôi URL
    """
    try:
        def _save_direct_bytes(data: bytes, ext: str) -> str:
            ext = ".jpg" if ext.lower() in [".jpeg", ".jpe"] else ext.lower()
            if ext not in [".jpg", ".png"]:
                ext = ".jpg"
            dst = os.path.join(out_dir, f"cover{ext}")
            with open(dst, "wb") as f:
                f.write(data)
            return dst

        if user_cover:
            if re.match(r"^https?://", user_cover, flags=re.I):
                data, ctype = _download_bytes(user_cover.strip())
                ext = _standardize_ext_from_mime(ctype or "")
                if not ext:
                    # fallback theo URL nếu thiếu/mơ hồ Content-Type
                    ext = _guess_ext_from_url(user_cover) or ".jpg"
                if ext in [".jpg", ".jpeg", ".png", ".jpe"]:
                    dst = _save_direct_bytes(data, ext)
                    print(f"Cover : downloaded -> {dst}")
                    return dst
                tmp_path = os.path.join(out_dir, f"cover_raw{ext or '.bin'}")
                with open(tmp_path, "wb") as f:
                    f.write(data)
                print(f"Cover : downloaded (raw) -> {tmp_path}")
                return convert_cover_to_epub_safe(tmp_path, out_dir, logger)
            else:
                src = os.path.abspath(user_cover)
                if not os.path.isfile(src):
                    logger.log(f"[WARN] Cover local path không tồn tại: {src}")
                    return None
                ext = os.path.splitext(src)[1].lower()
                if ext in [".jpg", ".jpeg", ".png", ".jpe"]:
                    dst = os.path.join(out_dir, f"cover{('.jpg' if ext in ['.jpeg','.jpe'] else ext)}")
                    shutil.copyfile(src, dst)
                    print(f"Cover : copied -> {dst}")
                    return dst
                tmp_path = os.path.join(out_dir, f"cover_raw{ext or '.bin'}")
                shutil.copyfile(src, tmp_path)
                print(f"Cover : copied (raw) -> {tmp_path}")
                return convert_cover_to_epub_safe(tmp_path, out_dir, logger)
        else:
            url = _find_cover_url_from_page(StoryUrl)
            if not url:
                logger.log("[WARN] Không tìm thấy ảnh bìa trên trang.")
                return None
            data, ctype = _download_bytes(url)
            ext = _standardize_ext_from_mime(ctype or "")
            if not ext:
                ext = _guess_ext_from_url(url) or ".jpg"
            if ext in [".jpg", ".jpeg", ".png", ".jpe"]:
                dst = _save_direct_bytes(data, ext)
                print(f"Cover : downloaded -> {dst}")
                return dst
            tmp_path = os.path.join(out_dir, f"cover_raw{ext or '.bin'}")
            with open(tmp_path, "wb") as f:
                f.write(data)
            print(f"Cover : downloaded (raw) -> {tmp_path}")
            return convert_cover_to_epub_safe(tmp_path, out_dir, logger)
    except Exception as e:
        logger.log(f"[WARN] Cover error: {e}")
        return None

def _cover_page_xhtml(img_filename: str) -> bytes:
    """Trang cover.xhtml để spine mở đầu."""
    html_str = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="vi">
<head><meta charset="utf-8"/><title>Cover</title></head>
<body style="margin:0;padding:0;">
  <div style="text-align:center;">
    <img src="../Images/{html.escape(img_filename)}" alt="Cover" style="max-width:100%;height:auto;"/>
  </div>
</body>
</html>
"""
    return html_str.encode("utf-8")

# ====================== TẠO OPF/NCX/NAV ======================

def _make_container_xml() -> bytes:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
    return xml.encode("utf-8")

def _make_nav_xhtml(book_title: str, nav_items: List[Tuple[str, str]]) -> bytes:
    lis = "\n".join(
        f'    <li><a href="{href}">{html.escape(label)}</a></li>'
        for href, label in nav_items
    )
    s = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="vi">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(book_title)}</title>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Mục lục</h1>
    <ol>
{lis}
    </ol>
  </nav>
</body>
</html>
'''
    return s.encode("utf-8")

def _make_opf(book_title: str, author: str, items: List[Tuple[str, str]],
              publisher: str = "Hishiro",
              tags=None,
              cover_image_href: Optional[str] = None,
              cover_media_type: Optional[str] = None,
              cover_page_href: Optional[str] = None,
              epub_target: str = EPUB_TARGET) -> bytes:
    '''
    make opf file
    
    '''
    
    is_epub3 = (epub_target == "epub3")

    manifest = []
    # Luôn có NCX cho Kobo/thiết bị cũ
    manifest.append('    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')

    # Cover image
    if cover_image_href and cover_media_type:
        if is_epub3:
            manifest.append(
                f'    <item id="cover-image" href="{cover_image_href}" media-type="{cover_media_type}" properties="cover-image"/>'
            )
        else:
            manifest.append(
                f'    <item id="cover-image" href="{cover_image_href}" media-type="{cover_media_type}"/>'
            )

    # Cover page
    if cover_page_href:
        manifest.append(f'    <item id="cover" href="{cover_page_href}" media-type="application/xhtml+xml"/>')

    # nav.xhtml cho EPUB3
    if is_epub3:
        manifest.append('    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')

    # Chapters
    for i, h in items:
        manifest.append(f'    <item id="{i}" href="{h}" media-type="application/xhtml+xml"/>')

    # Spine
    spine = []
    if cover_page_href:
        spine.append('    <itemref idref="cover"/>')
    for i, _ in items:
        spine.append(f'    <itemref idref="{i}"/>')

    # Metadata phụ — giữ meta name="cover" để Kobo chắc chắn nhận bìa
    extra_meta = ""
    extra_meta += subject_xml(tags)
    if cover_image_href:
        extra_meta += '    <meta name="cover" content="cover-image"/>\n'

    cover_ref = f'<reference type="cover" title="Cover" href="{cover_page_href}"/>' if cover_page_href else ''

    pkg_version = "3.0" if is_epub3 else "2.0"
    spine_toc_attr = "" if is_epub3 else ' toc="ncx"'

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package unique-identifier="BookId" version="{pkg_version}" xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{html.escape(book_title)}</dc:title>
    <dc:creator opf:role="aut">{html.escape(author or "Unknown")}</dc:creator>
    <dc:publisher>{html.escape(publisher)}</dc:publisher>
    <dc:language>vi</dc:language>
    <dc:identifier id="BookId">urn:uuid:{os.urandom(16).hex()}</dc:identifier>
{extra_meta}    <meta name="generator" content="StoryDownloader"/>
  </metadata>
  <manifest>
{chr(10).join(manifest)}
  </manifest>
  <spine{spine_toc_attr}>
{chr(10).join(spine)}
  </spine>
  <guide>
    {cover_ref}
  </guide>
</package>
"""
    return opf.encode("utf-8")

def _make_ncx(book_title: str, items: List[Tuple[str, str, str]]) -> bytes:
    '''
    make ncx file
    '''
    navpoints = []
    play_order = 1
    for i, href, label in items:
        navpoints.append(f"""    <navPoint id="{i}" playOrder="{play_order}">
      <navLabel><text>{html.escape(label)}</text></navLabel>
      <content src="{href}"/>
    </navPoint>""")
        play_order += 1
    ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{os.urandom(8).hex()}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(book_title)}</text></docTitle>
  <navMap>
{chr(10).join(navpoints)}
  </navMap>
</ncx>"""
    return ncx.encode("utf-8")

# ====================== TẠO EPUB ======================

def create_epub_from_html(book_title: str, author: str, out_dir: str, logger: Logger,
                          cover_path: Optional[str], tags=None) -> str:
    """Gom các *.xhtml/*.html trong out_dir -> tạo EPUB (cover nếu có).
       EPUB lưu ở OUTPUT_ROOT (không trong thư mục truyện)."""
    ext = ".xhtml" if SAVE_AS_XHTML else ".html"
    files = [f for f in os.listdir(out_dir) if f.lower().endswith(ext)]
    files.sort()
    if not files:
        raise RuntimeError("Không tìm thấy file HTML để tạo EPUB.")

    for f in files:
        logger.log(f"Readfile : {f} from {out_dir}")

    ensure_dir(OUTPUT_ROOT)
    epub_name = slugify_filename(book_title, allow_unicode=not USE_NO_DIACRITICS_FOLDER) + ".epub"
    epub_path = os.path.join(OUTPUT_ROOT, epub_name)

    cover_img_filename = None
    cover_media_type = None
    if cover_path and os.path.isfile(cover_path):
        ext_img = os.path.splitext(cover_path)[1].lower()
        if ext_img in [".jpeg", ".jpe"]: ext_img = ".jpg"
        if ext_img not in [".jpg",".png",".gif"]: ext_img = ".jpg"
        cover_img_filename = f"cover{ext_img}"
        cover_media_type = "image/jpeg" if ext_img==".jpg" else ("image/png" if ext_img==".png" else "image/gif")

    with zipfile.ZipFile(epub_path, "w") as zf:
        # mimetype (không nén)
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, b"application/epub+zip")

        # META-INF
        zf.writestr("META-INF/container.xml", _make_container_xml())

        # OEBPS/Images + cover.xhtml
        cover_page_href = None
        if cover_img_filename:
            with open(cover_path, "rb") as fp:
                zf.writestr(f"OEBPS/Images/{cover_img_filename}", fp.read())
            zf.writestr("OEBPS/Text/cover.xhtml", _cover_page_xhtml(cover_img_filename))
            cover_page_href = "Text/cover.xhtml"
            logger.log(f"Cover : embedded {cover_img_filename}")

        # OEBPS/Text/<chapters>
        item_list: List[Tuple[str,str]] = []
        nav_list:  List[Tuple[str,str,str]] = []

        for f in files:
            src_path = os.path.join(out_dir, f)
            dst_path = f"OEBPS/Text/{f}"
            with open(src_path, "rb") as fp:
                zf.writestr(dst_path, fp.read())
            item_id = os.path.splitext(f)[0].replace(".", "-")
            item_list.append((item_id, f"Text/{f}"))
            try:
                with open(src_path, "r", encoding="utf-8") as fp:
                    soup = BeautifulSoup(fp.read(), "html.parser")
                    chap_title = soup.find("h1").get_text(" ", strip=True)
            except Exception:
                chap_title = f
            nav_list.append((item_id, f"Text/{f}", chap_title))

        # cho cover đứng đầu spine / nav
        if cover_page_href:
            nav_list = [("cover", cover_page_href, "Bìa")] + nav_list

        # EPUB3: thêm nav.xhtml (vẫn giữ NCX cho Kobo)
        if EPUB_TARGET == "epub3":
            nav_items = [(href, label) for (_id, href, label) in nav_list]
            zf.writestr("OEBPS/nav.xhtml", _make_nav_xhtml(book_title, nav_items))

        # content.opf + toc.ncx
        zf.writestr(
            "OEBPS/content.opf",
            _make_opf(
                book_title, author, item_list,
                publisher="Hishiro",
                tags=tags,
                cover_image_href=(f"Images/{cover_img_filename}" if cover_img_filename else None),
                cover_media_type=cover_media_type,
                cover_page_href=cover_page_href,
                epub_target=EPUB_TARGET,
            )
        )
        zf.writestr("OEBPS/toc.ncx", _make_ncx(book_title, nav_list))

    logger.log(f"EPUB created: {epub_path}")
    return epub_path

def create_epub_from_txt(book_title: str, author: str, out_dir: str, logger: Logger,
                         cover_path: Optional[str], tags=None) -> str:
    """Đọc *.txt → bọc XHTML tối giản → EPUB (có cover nếu cung cấp)."""
    files = [f for f in os.listdir(out_dir) if f.lower().endswith(".txt")]
    files.sort()
    if not files:
        raise RuntimeError("Không tìm thấy file TXT để tạo EPUB.")

    width = len(str(len(files)))
    for idx, f in enumerate(files, 1):
        logger.log(f"Readfile : {f} from {out_dir}")
        path = os.path.join(out_dir, f)
        with open(path, "r", encoding="utf-8") as fp:
            raw = fp.read()
        parts = raw.splitlines()
        chap_title = parts[0].strip() if parts else "Chương"
        body = "\n".join(parts[2:] if len(parts) > 2 else parts)
        paras = [f"<p>{html.escape(x.strip())}</p>" for x in re.split(r"\n{2,}", body) if x.strip()]
        content_html = "".join(paras) if paras else "<p>(Trống)</p>"
        fname = f"{idx:0{width}d}.xhtml"
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as fp:
            fp.write(xhtml_wrap(chap_title, content_html))

    # Sau khi bọc XHTML, dùng pipeline HTML để tạo EPUB
    epub_path = create_epub_from_html(book_title, author, out_dir, logger, cover_path, tags=tags)
    return epub_path

# ====================== QUY TRÌNH THEO MODE ======================

def download_all(chapters: List[Dict[str,str]], out_dir: str, logger: Logger, save_html_flag: bool, save_txt_flag: bool) -> None:
    '''
    Tạo file HTML/TXT cho tất cả chương trên.
    '''
    
    total = len(chapters)
    width = len(str(total if total>0 else 1))
    for idx, ch in enumerate(chapters, 1):
        try:
            data = extract_chapter_content(ch["link"])
            ctitle = data["title"] or ch["title"]
            if save_html_flag:
                save_html(out_dir, idx, width, ctitle, data["content_html"], logger)
            if save_txt_flag:
                save_txt(out_dir, idx, width, ctitle, data["content_html"], logger)
        except Exception as e:
            logger.log(f"[WARN] Bỏ qua chương {idx} ({ch['title']}): {e}")

def main():
    StoryUrl = input("Nhập URL:").strip()
    cover_in = input("Nhập đường dẫn Cover (bỏ trống để tự lấy từ web): ").strip()
    mode = input("Chọn chế độ (1=HTML, 2=TXT, 3=HTML+EPUB, 4=TXT+EPUB, 5=HTML+TXT+EPUB): ").strip()

    # Hỏi target khi có tạo EPUB (mode 3/4/5)
    if mode in {"3","4","5"}:
        target_in = input("Chọn EPUB target (2=EPUB 2, 3=EPUB 3 compat) [2]: ").strip()
        global EPUB_TARGET
        EPUB_TARGET = "epub3" if target_in == "3" else "epub2"
        print(f"EPUB target: {EPUB_TARGET}")

    meta = getinfo(StoryUrl)
    book_title = meta["info"].get("title") or "Truyen"
    author     = meta["info"].get("author") or "Unknown"
    genre      = meta["info"].get("genre") or ""
    chapters   = meta["chapters"]

    out_dir = make_book_dir(book_title)
    logger  = Logger(os.path.join(out_dir, "log.txt"))
    logger.log(f"Output : {out_dir}")

    # Chuẩn bị cover (convert nếu cần)
    cover_path = prepare_cover(StoryUrl, cover_in, out_dir, logger)

    if mode == "1":
        download_all(chapters, out_dir, logger, True,  False)
    elif mode == "2":
        download_all(chapters, out_dir, logger, False, True)
    elif mode == "3":
        download_all(chapters, out_dir, logger, True,  False)
        print("Tạo EPUB từ file HTML...")
        create_epub_from_html(book_title, author, out_dir, logger, cover_path, tags=genre)
    elif mode == "4":
        download_all(chapters, out_dir, logger, False, True)
        print("Tạo EPUB từ file TXT...")
        create_epub_from_txt(book_title, author, out_dir, logger, cover_path, tags=genre)
    elif mode == "5":
        download_all(chapters, out_dir, logger, True,  True)
        print("Tạo EPUB từ file HTML...")
        create_epub_from_html(book_title, author, out_dir, logger, cover_path, tags=genre)
    else:
        print("Mode không hợp lệ. Vui lòng chọn 1..5.")

if __name__ == "__main__":
    main()
