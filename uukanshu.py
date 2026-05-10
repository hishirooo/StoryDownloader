from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, urlunparse, urljoin
import ssl, urllib3, re, json, html, os, unicodedata, zipfile, io, datetime, shutil, time
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
USE_CURL_CFFI = False
try:
    from curl_cffi import requests as curl_requests
    requests = curl_requests
    USE_CURL_CFFI = True
except ImportError:
    try:
        import requests
    except ImportError:
        os.system("pip install curl_cffi")
        from curl_cffi import requests as curl_requests
        requests = curl_requests
        USE_CURL_CFFI = True
import importlib, sys, glob

# Thử import Pillow cho xử lý ảnh bìa
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("⚠ Cảnh báo: Không tìm thấy thư viện Pillow. Không thể tối ưu/chuyển đổi cover sang JPEG.")

# =============== CẤU HÌNH ===============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 5
SLEEP_BETWEEN_CHAPS = 5
RETRY_STATUS = {429, 500, 502, 503, 504}
EPUB_TARGET = "epub3"

# Kích thước tối đa cho ảnh bìa (Kobo/e-reader thân thiện)
MAX_COVER_SIZE = (1600, 2400) # (width, height)

def _text(el) -> str:
    '''
    Lấy text từ thẻ BeautifulSoup, trả về chuỗi rỗng nếu lỗi.
    '''
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""

def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    '''
    Tải HTML (có chuyển domain + fallback verify=False khi SSLError).
    '''
    for k in range(tries):
        if k > 0:
            time.sleep(SLEEP_BETWEEN_PAGES)
        # Dùng impersonate='chrome110' khi curl_cffi hỗ trợ
        kwargs = {"headers": HEADERS, "timeout": TIMEOUT}
        if USE_CURL_CFFI:
            kwargs["impersonate"] = "chrome110"
        try:
            r = requests.get(url, **kwargs)
        except TypeError as e:
            if USE_CURL_CFFI and "impersonate" in str(e).lower():
                kwargs.pop("impersonate", None)
                r = requests.get(url, **kwargs)
            else:
                raise
        
        # Chỉ retry các lỗi server (5xx) và 429
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status() # Lần cuối mà vẫn lỗi thì raise
            time.sleep(backoff*(k+1)); continue
        
        # Nếu là lỗi client (4xx) khác 429, dừng retry và raise
        if 400 <= r.status_code < 500:
            r.raise_for_status() 

        r.raise_for_status() # Các status code 2xx thành công
        
        # requests trong curl_cffi có thể không có apparent_encoding, nên ta bypass lỗi này nếu gặp
        encoding = r.encoding if hasattr(r, 'encoding') else 'utf-8'
        if not encoding or encoding.lower() == "iso-8859-1":
            encoding = getattr(r, 'apparent_encoding', 'utf-8')
        
        return BeautifulSoup(r.text if getattr(r, 'text', None) else r.content.decode(encoding, 'ignore'), "html.parser")

#----------------------INFO TRUYỆN----------------------
def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    info = {
        "title": "Unknown",
        "genre": "N/A",
        "cover_url": "",
        "intro": "",
    }

    h1 = soup.find("h1", class_="booktitle")
    if h1: info["title"] = _text(h1)

    genre = soup.find("span",class_="blue").find_next("span") # thẻ span thứ 2
    if genre: info["genre"] = _text(genre)

    intro = soup.find("p",class_="bookintro")
    if intro: info["intro"] = _text(intro)

    img_cover = soup.find("img",class_="thumbnail")
    if img_cover: info["cover_url"] = img_cover["src"]  

    return info
    
def _get_list_chapters(soup: BeautifulSoup) -> List[Dict[str, str]]:
    chap_list = []
    div_list = soup.find("div",id="list-chapterAll")
    if not div_list: return []
    for li in div_list.find_all("dd"):
        a = li.find("a", href=True)
        if a: chap_list.append({"title": _text(a), "url": "https://uukanshu.cc" + a["href"]})

    return chap_list

def _get_chapter_content(soup: BeautifulSoup) -> str:
    """Lấy nội dung chương truyện."""
    content = soup.find("div", class_="readcotent bbb font-normal")
    if content: return _text(content)
    return ""

# Hàm getText để tương thích với main.py
def getText(url: str) -> Dict:
    soup = _fetch_html(url)
    info = _get_book_info(soup)
    chapters = _get_list_chapters(soup)
    return {
        "title": info["title"],
        "author": "Unknown",  # uukanshu không có author rõ ràng
        "chapters": chapters,
        "total_chapters": len(chapters),
        "cover_url": info["cover_url"],
        "intro": info["intro"],
    }

# Hàm fetch_chapter_content để tải nội dung chương với retry
def fetch_chapter_content(url: str, retries: int = 3) -> Dict:
    for attempt in range(retries):
        try:
            print(f"Đang tải chương từ {url} (lần thử {attempt + 1})...")
            soup = _fetch_html(url)
            title = _get_chapter_title(soup)
            content_html = _get_chapter_content_html(soup)
            print(f"✓ Đã tải thành công: {title}")
            time.sleep(SLEEP_BETWEEN_CHAPS)
            return {
                "title": title,
                "content_html": content_html,
            }
        except Exception as e:
            print(f"✗ Lỗi tải chương (lần {attempt + 1}): {e}")
            if attempt < retries - 1:
                time.sleep(1)  # Chờ 1 giây trước khi retry
            else:
                print(f"⚠ Không tải được chương sau {retries} lần thử.")
                return {
                    "title": "Chương lỗi",
                    "content_html": "<p>Nội dung không tải được.</p>",
                }

def _get_chapter_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1: return _text(h1)
    return "Chương"

def _get_chapter_content_html(soup: BeautifulSoup) -> str:
    content = soup.find("div", class_="readcotent bbb font-normal")
    if content:
        # Loại bỏ script / quảng cáo trong nội dung
        for node in content.find_all(["script", "style"]):
            node.decompose()

        # Giữ lại dòng mới theo thẻ br và lọc rác
        raw_text = content.get_text("\n", strip=True)
        raw_text = _clean_text(raw_text)

        lines = [line.strip() for line in raw_text.splitlines()]
        paragraphs = []
        current = []
        for line in lines:
            if not line:
                if current:
                    paragraphs.append(" ".join(current))
                    current = []
            else:
                current.append(line)
        if current:
            paragraphs.append(" ".join(current))

        html_content = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs if p)
        return html_content
    return "<p>Không có nội dung</p>"

def _clean_text(text: str) -> str:
    # Lọc rác: bỏ dòng quảng cáo, credit, v.v.
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line: continue
        # Bỏ dòng chứa từ khóa rác
        if re.search(r'(?i)(quảng cáo|tặng phiếu|bản quyền|nguồn|đăng tại|uukanshu|chương tiếp theo|chương trước|copyright|本书首发|loadAdv|广告|首发|本站|site|www\.)', line):
            continue
        # Bỏ dòng quá ngắn hoặc chỉ có ký tự đặc biệt
        if len(line) < 10 and not re.search(r'[a-zA-Z0-9\u4e00-\u9fff]', line):
            continue
        cleaned.append(line)
    return '\n\n'.join(cleaned)

# Hàm tải cover
def _download_cover(cover_url: str) -> Tuple[bytes, str]:
    if not cover_url: return None, None
    try:
        kwargs = {"headers": HEADERS, "timeout": TIMEOUT}
        if USE_CURL_CFFI:
            kwargs["impersonate"] = "chrome110"
        try:
            r = requests.get(cover_url, **kwargs)
        except TypeError as e:
            if USE_CURL_CFFI and "impersonate" in str(e).lower():
                kwargs.pop("impersonate", None)
                r = requests.get(cover_url, **kwargs)
            else:
                raise
        r.raise_for_status()
        content = r.content
        ext = ".jpg"  # mặc định
        if "png" in r.headers.get("content-type", "").lower(): ext = ".png"
        elif "gif" in r.headers.get("content-type", "").lower(): ext = ".gif"
        elif "webp" in r.headers.get("content-type", "").lower(): ext = ".webp"
        return content, ext
    except Exception as e:
        print(f"Lỗi tải cover: {e}")
        return None, None

# Hàm resize cover nếu có Pillow
def _resize_cover(content: bytes, ext: str) -> Tuple[bytes, str]:
    if not HAS_PILLOW: return content, ext
    try:
        img = Image.open(io.BytesIO(content))
        img = img.convert("RGB")  # sang JPEG
        img.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=85)
        return output.getvalue(), ".jpg"
    except Exception as e:
        print(f"Lỗi resize cover: {e}")
        return content, ext

# Hàm lưu HTML từng chương để cache
def _save_chapter_html(chap: Dict, idx: int, out_dir: str):
    html_dir = os.path.join(out_dir, "html")
    os.makedirs(html_dir, exist_ok=True)
    fname = f"{idx:04d}.html"
    html_path = os.path.join(html_dir, fname)
    if os.path.exists(html_path):
        print(f"HTML đã tồn tại: {fname}")
        return html_path
    data = fetch_chapter_content(chap["url"])
    html_content = f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="utf-8">
    <title>{html.escape(data['title'])}</title>
</head>
<body>
    <h1>{html.escape(data['title'])}</h1>
    {data['content_html']}
</body>
</html>"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Đã lưu HTML: {fname} - {data['title']}")
    return html_path

# Hàm lưu txt gộp
def _save_txt_combined(book_title: str, chapters: List[Dict], out_dir: str, start: int = 1, end: Optional[int] = None):
    if end is None: end = len(chapters)
    txt_path = os.path.join(out_dir, f"{book_title}.txt")
    html_dir = os.path.join(out_dir, "html")
    with open(txt_path, "w", encoding="utf-8") as f:
        for i in range(start-1, min(end, len(chapters))):
            chap = chapters[i]
            html_path = os.path.join(html_dir, f"{i+1:04d}.html")
            if os.path.exists(html_path):
                print(f"Sử dụng HTML cache cho chương {i+1}")
                with open(html_path, "r", encoding="utf-8") as hf:
                    soup = BeautifulSoup(hf.read(), "html.parser")
                title = soup.find("h1").get_text() if soup.find("h1") else chap["title"]
                content_html = soup.find("body").decode_contents() if soup.find("body") else ""
            else:
                data = fetch_chapter_content(chap["url"])
                title = data['title']
                content_html = data['content_html']
                # Lưu HTML
                _save_chapter_html(chap, i+1, out_dir)
            f.write(f"{title}\n\n")
            soup = BeautifulSoup(content_html, "html.parser")
            text = soup.get_text("\n", strip=True)
            f.write(text + "\n\n")
            print(f"Đã xử lý chương {i+1}: {title}")
    print(f"Đã lưu TXT gộp: {txt_path}")

# Hàm lưu txt tách chương
def _save_txt_split(book_title: str, chapters: List[Dict], out_dir: str, start: int = 1, end: Optional[int] = None):
    if end is None: end = len(chapters)
    txt_dir = os.path.join(out_dir, "txt")
    os.makedirs(txt_dir, exist_ok=True)
    html_dir = os.path.join(out_dir, "html")
    for i in range(start-1, min(end, len(chapters))):
        chap = chapters[i]
        fname = f"{i+1:04d}.txt"
        txt_path = os.path.join(txt_dir, fname)
        if os.path.exists(txt_path):
            print(f"TXT đã tồn tại: {fname}")
            continue
        html_path = os.path.join(html_dir, f"{i+1:04d}.html")
        if os.path.exists(html_path):
            print(f"Sử dụng HTML cache cho chương {i+1}")
            with open(html_path, "r", encoding="utf-8") as hf:
                soup = BeautifulSoup(hf.read(), "html.parser")
            title = soup.find("h1").get_text() if soup.find("h1") else chap["title"]
            content_html = soup.find("body").decode_contents() if soup.find("body") else ""
        else:
            data = fetch_chapter_content(chap["url"])
            title = data['title']
            content_html = data['content_html']
            # Lưu HTML
            _save_chapter_html(chap, i+1, out_dir)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"{title}\n\n")
            soup = BeautifulSoup(content_html, "html.parser")
            text = soup.get_text("\n", strip=True)
            f.write(text + "\n")
        print(f"Đã lưu TXT: {fname} - {title}")

# Hàm lưu epub
def _save_epub(book_title: str, author: str, chapters: List[Dict], out_dir: str, cover_bytes: bytes = None, cover_ext: str = ".jpg", start: int = 1, end: Optional[int] = None):
    if end is None: end = len(chapters)
    import epub_builder
    epub_name = f"{book_title}.epub"
    epub_path = os.path.join(out_dir, epub_name)
    selected_chapters = chapters[start-1:end]
    html_cache_dir = os.path.join(out_dir, "html")
    # Đảm bảo tất cả HTML đã được tải
    for i in range(start-1, min(end, len(chapters))):
        chap = chapters[i]
        _save_chapter_html(chap, i+1, out_dir)
    epub_builder.create_epub(
        book_url="",
        book_title=book_title,
        author=author,
        chapters=selected_chapters,
        fetch_fn=fetch_chapter_content,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        out_epub_path=epub_path,
        html_cache_dir=html_cache_dir
    )
    print(f"Đã tạo EPUB: {epub_path}")

# Hàm chính
def main():
    print("Ứng dụng tải truyện từ uukanshu.cc")
    url = input("Nhập URL truyện: ").strip()
    if not url.startswith("http"):
        url = "https://" + url

    # Lấy thông tin truyện
    print("Đang lấy thông tin truyện...")
    data = getText(url)
    title = data["title"]
    author = data["author"]
    chapters = data["chapters"]
    cover_url = data["cover_url"]

    print(f"Tiêu đề: {title}")
    print(f"Tác giả: {author}")
    print(f"Số chương: {len(chapters)}")

    # Tải cover
    cover_bytes, cover_ext = None, ".jpg"
    if cover_url:
        print(f"Đang tải cover từ {cover_url}...")
        cover_bytes, cover_ext = _download_cover(cover_url)
        if cover_bytes:
            print("Đang xử lý cover (resize, convert)...")
            cover_bytes, cover_ext = _resize_cover(cover_bytes, cover_ext)
            print("✓ Đã xử lý cover.")
        else:
            print("✗ Không tải được cover.")
    else:
        print("Không có cover URL.")

    # Tạo thư mục output
    out_dir = os.path.join("output", title.replace("/", "-").replace("\\", "-"))
    os.makedirs(out_dir, exist_ok=True)
    print(f"Thư mục lưu: {out_dir}")

    # Menu
    while True:
        print("\n" + "="*50)
        print("Menu lựa chọn:")
        print("[1] Lưu txt gộp text")
        print("[2] Lưu txt tách chương")
        print("[3] Lưu epub (mặc định)")
        print("[4] Tải từ chương X đến chương Y")
        print("[0] Thoát")
        choice = input("Chọn (0-4): ").strip()

        if choice == "0":
            print("Thoát ứng dụng.")
            break
        elif choice == "1":
            print("Bắt đầu lưu TXT gộp...")
            _save_txt_combined(title, chapters, out_dir)
        elif choice == "2":
            print("Bắt đầu lưu TXT tách chương...")
            _save_txt_split(title, chapters, out_dir)
        elif choice == "3":
            print("Bắt đầu tạo EPUB...")
            _save_epub(title, author, chapters, out_dir, cover_bytes, cover_ext)
        elif choice == "4":
            try:
                start = int(input("Chương bắt đầu: ").strip())
                end_input = input("Chương kết thúc (Enter để hết): ").strip()
                end = int(end_input) if end_input else None
                sub_choice = input("Chọn định dạng (1=txt gộp, 2=txt tách, 3=epub): ").strip()
                if sub_choice == "1":
                    print(f"Bắt đầu lưu TXT gộp từ chương {start} đến {end or len(chapters)}...")
                    _save_txt_combined(title, chapters, out_dir, start, end)
                elif sub_choice == "2":
                    print(f"Bắt đầu lưu TXT tách từ chương {start} đến {end or len(chapters)}...")
                    _save_txt_split(title, chapters, out_dir, start, end)
                elif sub_choice == "3":
                    print(f"Bắt đầu tạo EPUB từ chương {start} đến {end or len(chapters)}...")
                    _save_epub(title, author, chapters, out_dir, cover_bytes, cover_ext, start, end)
                else:
                    print("Lựa chọn không hợp lệ.")
            except ValueError:
                print("Vui lòng nhập số hợp lệ.")
        else:
            print("Lựa chọn không hợp lệ. Thử lại.")

if __name__ == "__main__":
    main()
