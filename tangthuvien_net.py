# -*- coding: utf-8 -*-
"""
tangthuvien_net.py — Trình tải chương & tạo EPUB (TTV)

CHỨC NĂNG CHÍNH
1) Tải HTML
2) Tải TXT
3) Tải HTML + Tạo EPUB (đọc lại HTML đã tải)
4) Tải TXT  + Tạo EPUB (từ TXT -> bọc HTML đơn giản -> EPUB)
5) Tải HTML + TXT + Tạo EPUB (EPUB dùng HTML đã tải)

COVER
- Sau khi nhập URL, nhập thêm đường dẫn Cover (local path hoặc URL ảnh).
- Nếu bỏ trống: tự lấy ảnh bìa từ trang theo DOM:
    <div class="book-img"><a id="bookImg"><img src="..."></a></div>
- Nếu ảnh không thuộc định dạng EPUB-friendly (jpeg/png), sẽ **convert 1 lần** sang JPEG (cần Pillow).

GHI LOG
- Thư mục đầu ra: .\Output\<Tên Truyện>  (không dấu hoặc có dấu tuỳ cấu hình)
- EPUB sẽ lưu ở: .\Output\<TênTruyen>.epub (theo yêu cầu)
- Mỗi bước đều in log và lưu vào file: Output/<Tên Truyện>/log.txt
  * Khi tải:   Saved : 0001.xhtml - <Tiêu đề chương>
  * Khi tạo:   Readfile : 0001.xhtml from Output/<Tên Truyện>
"""
from __future__ import annotations
from typing import Optional, List, Dict, Tuple
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, urljoin
import requests, ssl, urllib3, re, json, html, os, unicodedata, zipfile, io, datetime, shutil, mimetypes

from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# ====================== CẤU HÌNH ======================

BASE = "https://truyen.tangthuvien.vn"     # Mirror ổn định
OUTPUT_ROOT = os.path.join(os.getcwd(), "Output")
USE_NO_DIACRITICS_FOLDER = True            # True: thư mục không dấu; False: giữ nguyên dấu
SAVE_AS_XHTML = True                       # Lưu HTML dưới dạng .xhtml

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

# ====================== SESSION + TLS ======================

class TLSAdapter(HTTPAdapter):
    """Adapter ép TLS >= 1.2 và thêm retry hợp lý."""
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
    """Chuyển *.tangthuvien.net -> truyen.tangthuvien.vn (ổn định hơn)."""
    u = urlparse(url)
    host = (u.netloc or "").lower()
    if host.endswith("tangthuvien.net"):
        u = u._replace(scheme="https", netloc="truyen.tangthuvien.vn")
        return urlunparse(u)
    return url

def _fetch_html(url: str) -> BeautifulSoup:
    """Tải HTML (có chuyển domain + fallback verify=False khi gặp SSLError)."""
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

def _download_bytes(url: str) -> Tuple[bytes, Optional[str]]:
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
    """Chuẩn hoá tên tệp/thư mục hợp lệ Windows + bỏ dấu gạch '-' theo yêu cầu."""
    name = (name or "").strip().replace("—","-").replace("–","-")
    # BỎ gạch ngang trong tên
    name = name.replace("-", "").strip()
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
    """Lấy title/author/genre/status theo layout quen thuộc của TTV (có fallback)."""
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
    """Đọc <meta name="book_detail" content="..."> để lấy story_id (hỗ trợ content là số/JSON)."""
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
    """In thông tin truyện + trả dict nếu muốn dùng tiếp."""
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

# ====================== TRÍCH & LÀM SẠCH NỘI DUNG CHƯƠNG ======================

_UNWANTED_SELECTORS = [
    "script","style","noscript","iframe","ins",
    ".left-control","ul.left-control",".right-control",".top-control",
    ".panel-box",".panel-catalog",".chapter-control",".chapter-controls",
    "#chapter-tool","#chapter-toolbar",".chapter-toolbar",
    ".chapter-actions",".chapter-nav",".chapter-buttons",
    ".social",".share",".zalo",".fb",".twitter",".tiktok",
    ".ads",".ad","[class*='ads']","[id*='ads']",".banner",
    ".donate",".donation",".donate-box",".vote",".rating",".coin",".voucher",
    ".breadcrumb",".toolbox",".copyright",
    ".hidden",".clearfix",".icon-control",".fa",".glyphicon",
    "header","footer","form","button","svg",
    ".more-chap","a.more-chap","[onclick*='openNextChap']",
    "[class^='box-adv']","[class*=' box-adv']",
]
_JUNK_PATTERNS = [
    r"\btặng\s+phiếu\b", r"\bdonate\b", r"\bủng hộ\b", r"\bnhấn\b", r"\bbấm\b",
    r"\bđọc\s+(tiếp|full)\b", r"https?://", r"\btangthuvien\b", r"\bquảng\s*cáo\b",
    r"\blike\b", r"\bshare\b", r"\btheo dõi\b", r"\bzalo\b", r"\bapp\b",
    r"\bđăng nhập\b", r"\bđăng ký\b", r"\bcomment\b", r"\bbình luận\b",
]
_JUNK_RE = re.compile("|".join(_JUNK_PATTERNS), flags=re.I)
_CONTENT_SELECTORS = [
    "#chapter-content",".chapter-content",".chapter-c-content",
    ".box-chap",".reading-content",".read-content","article",
]
_TITLE_SELECTORS = [
    "h1.chapter-title","h2.chapter-title","div.chapter h2",".chapter h2",
    ".chapter .title","h1.title","h2.title","h1","h2",
    ".chapter-name",".chap-name",".entry-title",".book-chapter h1",".book-chapter h2",
]

def _select_chapter_container(soup: BeautifulSoup) -> BeautifulSoup:
    for sel in _CONTENT_SELECTORS:
        node = soup.select_one(sel)
        if node: return node
    return soup

def _dom_cleanup(container: BeautifulSoup) -> BeautifulSoup:
    cleaned = BeautifulSoup(str(container), "html.parser")
    for css in _UNWANTED_SELECTORS:
        for el in cleaned.select(css):
            el.decompose()
    return cleaned

def _is_junk_line(text: str) -> bool:
    if not text or not text.strip(): return True
    if _JUNK_RE.search(text): return True
    if len(text.strip()) < 3: return True
    return False

def _extract_chapter_title(soup: BeautifulSoup, container: BeautifulSoup) -> str:
    for h in container.select("h1, h2, h3"):
        t = h.get_text(" ", strip=True)
        if re.search(r"\bChương\b\s*\d+", t, flags=re.I): return t
    for sel in _TITLE_SELECTORS:
        el = soup.select_one(sel) or container.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t: return t
    og = soup.find("meta", attrs={"property":"og:title"}) or soup.find("meta", attrs={"name":"og:title"})
    if og and og.get("content"): return og["content"].strip()
    bc = soup.select(".breadcrumb li, nav.breadcrumb li, .breadcrumbs li")
    if bc:
        last = bc[-1].get_text(" ", strip=True)
        if last: return last
    if soup.title: return soup.title.get_text(" ", strip=True)
    return ""

def _collect_text_blocks(cleaned: BeautifulSoup) -> List[str]:
    """
    Tách đoạn văn thân thiện hơn:
    - Ưu tiên <p> thành từng đoạn
    - Nếu không có <p>: dựa trên <br> và kết thúc block (</p>, </div>, </li>, </h*>)
    - Loại rác theo regex _JUNK_RE
    """
    texts: List[str] = []

    # 1) Nếu có <p>, lấy từng <p>
    ps = cleaned.find_all("p")
    for p in ps:
        t = p.get_text(" ", strip=True)
        if not _is_junk_line(t): texts.append(t)

    if not texts:
        # 2) Không có <p>: dùng HTML + quy tắc xuống dòng/đóng block để chia đoạn
        inner_html = cleaned.decode_contents()
        inner_html = re.sub(r"(?i)<br\s*/?>", "\n", inner_html)  # <br> -> \n
        inner_html = re.sub(r"(?i)</p>|</div>|</li>|</h\d>", "\n\n", inner_html)  # kết thúc block -> \n\n
        inner_text = BeautifulSoup(inner_html, "html.parser").get_text("\n", strip=True)

        # 2+ dòng trống => ngắt đoạn
        paras = re.split(r"\n{2,}", inner_text)
        for para in paras:
            ptxt = para.strip()
            if not ptxt: continue
            # Ghép các dòng đơn thành 1 đoạn
            ptxt = re.sub(r"[ \t]*\n[ \t]*", " ", ptxt)
            if not _is_junk_line(ptxt):
                texts.append(ptxt)

    # Loại rác đầu/cuối
    while texts and _is_junk_line(texts[0]): texts.pop(0)
    while texts and _is_junk_line(texts[-1]): texts.pop()
    return texts

def extract_chapter_content(chapter_url: str) -> dict:
    soup = _fetch_html(chapter_url)
    container = _select_chapter_container(soup)
    cleaned = _dom_cleanup(container)
    title = _extract_chapter_title(soup, cleaned)
    texts = _collect_text_blocks(cleaned)
    content_html = "".join(f"<p>{html.escape(t)}</p>" for t in texts) if texts else "<p>(Không tìm thấy nội dung chương)</p>"
    return {"title": title, "content_html": content_html}

# ====================== LƯU FILE (HTML/TXT) ======================

def make_book_dir(book_title: str) -> str:
    """Tạo thư mục Output/<Tên Truyện> theo cấu hình có/không dấu."""
    folder_name = slugify_filename(book_title, allow_unicode=not USE_NO_DIACRITICS_FOLDER)
    out_dir = os.path.join(OUTPUT_ROOT, folder_name)
    ensure_dir(out_dir)
    return out_dir

def xhtml_wrap(title: str, body_html: str) -> str:
    """Khung XHTML tối giản (để dùng cho EPUB)."""
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
    ext = "xhtml" if SAVE_AS_XHTML else "html"
    fname = f"{idx:0{width}d}.{ext}"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(xhtml_wrap(chapter_title, content_html))
    logger.log(f"Saved : {fname} - {chapter_title}")
    return fpath

def save_txt(out_dir: str, idx: int, width: int, chapter_title: str, content_html: str, logger: Logger) -> str:
    fname = f"{idx:0{width}d}.txt"
    fpath = os.path.join(out_dir, fname)
    soup = BeautifulSoup(content_html, "html.parser")
    text = soup.get_text("\n", strip=True)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(chapter_title + "\n\n" + text + "\n")
    logger.log(f"Saved : {fname} - {chapter_title}")
    return fpath

# ====================== COVER: LẤY, CHUẨN HOÁ (CONVERT), NHÚNG EPUB ======================

def _guess_mime_from_ext(path_or_url: str) -> Tuple[str, str]:
    """Đoán (ext, mime) từ tên file. Mặc định .jpg nếu không rõ."""
    ext = os.path.splitext(path_or_url)[1].lower()
    if ext in [".jpg", ".jpeg"]:
        return ".jpg", "image/jpeg"
    if ext == ".png":
        return ".png", "image/png"
    if ext == ".gif":
        return ".gif", "image/gif"
    if ext == ".webp":
        return ".webp", "image/webp"
    return ".jpg", "image/jpeg"

def _standardize_ext_from_mime(mime: str) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    if mime == "image/png": return ".png"
    if mime == "image/jpeg": return ".jpg"
    if mime == "image/gif": return ".gif"
    if mime == "image/webp": return ".webp"
    return ".jpg"

def convert_cover_to_epub_safe(in_path: str, out_dir: str, logger: Logger) -> str:
    """
    Chỉ convert khi ảnh KHÔNG phải jpg/png.
    - Ưu tiên JPEG (phổ biến nhất). Nếu ảnh có alpha sẽ flatten nền trắng.
    - Yêu cầu Pillow: pip install pillow
    """
    ext = os.path.splitext(in_path)[1].lower()
    if ext in [".jpg", ".jpeg", ".png"]:
        # Không convert nữa
        logger.log("Cover : format already OK, skip convert.")
        # Nếu là .jpeg -> đổi tên thành cover.jpg cho gọn
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
    - Nếu định dạng đã hợp lệ (jpg/png) => lưu trực tiếp 'cover.jpg|png' (KHÔNG convert)
    - Nếu không hợp lệ (webp/gif/khác)   => lưu 'cover_raw.ext' rồi convert -> 'cover.jpg'
    """
    try:
        def _save_direct_bytes(data: bytes, ext: str) -> str:
            ext = ".jpg" if ext.lower() in [".jpeg", ".jpe"] else ext.lower()
            if ext not in [".jpg", ".png"]:
                ext = ".jpg"
            dst = os.path.join(out_dir, f"cover{ext}")
            with open(dst, "wb") as f:
                f.write(data)
            logger.log(f"Cover : downloaded -> {dst}")
            return dst

        if user_cover:
            if re.match(r"^https?://", user_cover, flags=re.I):
                data, ctype = _download_bytes(user_cover.strip())
                ext = _standardize_ext_from_mime(ctype or "") or ".jpg"
                if ext in [".jpg", ".jpeg", ".png", ".jpe"]:
                    return _save_direct_bytes(data, ext)
                tmp_path = os.path.join(out_dir, f"cover_raw{ext or '.bin'}")
                with open(tmp_path, "wb") as f:
                    f.write(data)
                logger.log(f"Cover : downloaded (raw) -> {tmp_path}")
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
                    logger.log(f"Cover : copied -> {dst}")
                    return dst
                tmp_path = os.path.join(out_dir, f"cover_raw{ext or '.bin'}")
                shutil.copyfile(src, tmp_path)
                logger.log(f"Cover : copied (raw) -> {tmp_path}")
                return convert_cover_to_epub_safe(tmp_path, out_dir, logger)
        else:
            url = _find_cover_url_from_page(StoryUrl)
            if not url:
                logger.log("[WARN] Không tìm thấy ảnh bìa trên trang.")
                return None
            data, ctype = _download_bytes(url)
            ext = _standardize_ext_from_mime(ctype or "") or ".jpg"
            if ext in [".jpg", ".jpeg", ".png", ".jpe"]:
                return _save_direct_bytes(data, ext)
            tmp_path = os.path.join(out_dir, f"cover_raw{ext or '.bin'}")
            with open(tmp_path, "wb") as f:
                f.write(data)
            logger.log(f"Cover : downloaded (raw) -> {tmp_path}")
            return convert_cover_to_epub_safe(tmp_path, out_dir, logger)
    except Exception as e:
        logger.log(f"[WARN] Cover error: {e}")
        return None

def _cover_page_xhtml(img_filename: str) -> bytes:
    """Tạo trang cover.xhtml tham chiếu ảnh ../Images/<img_filename>."""
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

# ====================== TẠO EPUB ======================

def _zip_write_uncompressed(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    zi = zipfile.ZipInfo(arcname)
    zi.compress_type = zipfile.ZIP_STORED
    zf.writestr(zi, data)

def _make_container_xml() -> bytes:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
    return xml.encode("utf-8")

def _make_opf(book_title: str, author: str, items: List[Tuple[str, str]],
              publisher: str = "Hishiro",
              cover_image_href: Optional[str] = None,
              cover_media_type: Optional[str] = None,
              cover_page_href: Optional[str] = None) -> bytes:
    """
    items: list of (id, href) e.g., ('ch0001','Text/0001.xhtml')
    """
    manifest = []
    # toc.ncx
    manifest.append('    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
    # cover image
    if cover_image_href and cover_media_type:
        manifest.append(f'    <item id="cover-image" href="{cover_image_href}" media-type="{cover_media_type}"/>')
    # cover page
    if cover_page_href:
        manifest.append(f'    <item id="cover" href="{cover_page_href}" media-type="application/xhtml+xml"/>')
    # chapters
    for i, h in items:
        manifest.append(f'    <item id="{i}" href="{h}" media-type="application/xhtml+xml"/>')

    # Spine: cover trước, rồi chapters
    spine = []
    if cover_page_href:
        spine.append('    <itemref idref="cover"/>')
    for i, _ in items:
        spine.append(f'    <itemref idref="{i}"/>')

    # Metadata
    extra_meta = ""
    if cover_image_href:
        extra_meta += '    <meta name="cover" content="cover-image"/>\n'

    cover_ref = f'<reference type="cover" title="Cover" href="{cover_page_href}"/>' if cover_page_href else ''

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package unique-identifier="BookId" version="2.0" xmlns="http://www.idpf.org/2007/opf">
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
  <spine toc="ncx">
{chr(10).join(spine)}
  </spine>
  <guide>
    {cover_ref}
  </guide>
</package>
"""
    return opf.encode("utf-8")

def _make_ncx(book_title: str, items: List[Tuple[str, str, str]]) -> bytes:
    """
    items: list of (id, href, navLabel) for chapters
    """
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

def create_epub_from_html(book_title: str, author: str, out_dir: str, logger: Logger, cover_path: Optional[str]) -> str:
    """Gom tất cả *.xhtml/*.html trong out_dir -> tạo EPUB, kèm cover nếu có.
       LƯU Ý: EPUB sẽ lưu ở OUTPUT_ROOT (không nằm trong thư mục truyện)."""
    ext = ".xhtml" if SAVE_AS_XHTML else ".html"
    files = [f for f in os.listdir(out_dir) if f.lower().endswith(ext)]
    files.sort()
    if not files:
        raise RuntimeError("Không tìm thấy file HTML để tạo EPUB.")

    for f in files:
        logger.log(f"Readfile : {f} from {out_dir}")

    epub_name = slugify_filename(book_title, allow_unicode=not USE_NO_DIACRITICS_FOLDER) + ".epub"
    epub_path = os.path.join(OUTPUT_ROOT, epub_name)

    cover_img_filename = None
    cover_media_type = None

    if cover_path and os.path.isfile(cover_path):
        # chuẩn hoá tên cover trong EPUB: cover.<ext>
        ext_img = os.path.splitext(cover_path)[1].lower()
        if ext_img in [".jpeg", ".jpe"]: ext_img = ".jpg"
        if ext_img not in [".jpg",".png",".gif"]:
            ext_img = ".jpg"
        cover_img_filename = f"cover{ext_img}"
        cover_media_type = "image/jpeg" if ext_img==".jpg" else ("image/png" if ext_img==".png" else "image/gif")

    with zipfile.ZipFile(epub_path, "w") as zf:
        # mimetype
        _zip_write_uncompressed(zf, "mimetype", b"application/epub+zip")
        # META-INF
        zf.writestr("META-INF/container.xml", _make_container_xml())

        # OEBPS: viết cover image + cover.xhtml nếu có
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

        # nếu có cover, chèn navpoint 'cover' lên đầu
        if cover_page_href:
            nav_list = [("cover", cover_page_href, "Bìa")] + nav_list

        # content.opf
        zf.writestr(
            "OEBPS/content.opf",
            _make_opf(
                book_title, author, item_list,
                publisher="Hishiro",
                cover_image_href=(f"Images/{cover_img_filename}" if cover_img_filename else None),
                cover_media_type=cover_media_type,
                cover_page_href=cover_page_href
            )
        )
        # toc.ncx
        zf.writestr("OEBPS/toc.ncx", _make_ncx(book_title, nav_list))

    logger.log(f"EPUB created: {epub_path}")
    return epub_path

def create_epub_from_txt(book_title: str, author: str, out_dir: str, logger: Logger, cover_path: Optional[str]) -> str:
    """Đọc *.txt rồi bọc XHTML tối giản -> EPUB (có cover nếu cung cấp)."""
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

    # Sau khi đổi hết sang XHTML, dùng chung hàm HTML->EPUB (có cover)
    epub_path = create_epub_from_html(book_title, author, out_dir, logger, cover_path)
    return epub_path

# ====================== QUY TRÌNH THEO MODE ======================

def download_all(chapters: List[Dict[str,str]], out_dir: str, logger: Logger, save_html_flag: bool, save_txt_flag: bool) -> None:
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

    meta = getinfo(StoryUrl)
    book_title = meta["info"].get("title") or "Truyen"
    author     = meta["info"].get("author") or "Unknown"
    chapters   = meta["chapters"]

    out_dir = make_book_dir(book_title)
    logger  = Logger(os.path.join(out_dir, "log.txt"))
    logger.log(f"Output : {out_dir}")

    # Chuẩn bị ảnh bìa (có thể convert nếu cần)
    cover_path = prepare_cover(StoryUrl, cover_in, out_dir, logger)

    if mode == "1":
        download_all(chapters, out_dir, logger, True,  False)
    elif mode == "2":
        download_all(chapters, out_dir, logger, False, True)
    elif mode == "3":
        download_all(chapters, out_dir, logger, True,  False)
        create_epub_from_html(book_title, author, out_dir, logger, cover_path)
    elif mode == "4":
        download_all(chapters, out_dir, logger, False, True)
        create_epub_from_txt(book_title, author, out_dir, logger, cover_path)
    elif mode == "5":
        download_all(chapters, out_dir, logger, True,  True)
        create_epub_from_html(book_title, author, out_dir, logger, cover_path)
    else:
        print("Mode không hợp lệ. Vui lòng chọn 1..5.")

if __name__ == "__main__":
    main()
