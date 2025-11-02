from bs4 import BeautifulSoup
from typing import Optional, List, Dict
from urllib.parse import urljoin
import requests, re, html, os, unicodedata, zipfile, glob, time, datetime as dt

# Cố gắng import Pillow
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("⚠️ Thiếu thư viện Pillow. Không thể tối ưu hóa/resize cover ảnh bìa.")


HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}
EPUB_TARGET = "epub3"   # mặc định; khi build sẽ hỏi lại

MAX_COVER_SIZE = (1600, 2400)  # Resize tối đa (w,h) cho cover


def _text(el) -> str:
    """Lấy text an toàn từ thẻ BeautifulSoup."""
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""


def _slugify_vi(s: str) -> str:
    """Slug ASCII an toàn cho tên file (Windows/Kobo thân thiện)."""
    s = (s or "book").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-_")
    return (s or "book")[:80]


def _fetch_html(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    """Tải HTML với retry hợp lý (dùng cho các trang HTML đầy đủ)."""
    for k in range(tries):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff * (k + 1))
            continue
        if 400 <= r.status_code < 500:
            r.raise_for_status()
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        soup.base_url = url  # lưu base cho urljoin
        return soup
    return BeautifulSoup("", "html.parser")


def _fetch_api_fragment(url: str, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    """
    Tải API JSON (vd: /get/listchap/...) rồi bóc trường 'data' (HTML fragment)
    và parse lại bằng BeautifulSoup.
    """
    for k in range(tries):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff * (k + 1))
            continue
        if 400 <= r.status_code < 500:
            r.raise_for_status()
        r.raise_for_status()
        # API trả JSON {"data":"<div class='clearfix'> ... </div>"}
        try:
            payload = r.json()
            html_fragment = payload.get("data", "")
        except ValueError:
            # không phải JSON: fallback coi như HTML
            html_fragment = r.text
        soup = BeautifulSoup(html_fragment, "html.parser")
        soup.base_url = url  # dùng domain API để join href tương đối
        return soup
    return BeautifulSoup("", "html.parser")


def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    """Lấy thông tin sách từ trang chính của truyện."""
    info = {}
    info["title"] = _text(soup.find("h1", itemprop="name"))

    info_text = soup.find("div", class_="book-info-text")
    li_nodes = info_text.find_all("li") if info_text else []

    author = li_nodes[0].find("a") if len(li_nodes) >= 1 else None
    info["author"] = _text(author) if author else "N/A"

    genres = []
    if len(li_nodes) >= 2:
        for g in li_nodes[1].find_all("a"):
            genres.append(_text(g))
    info["genres"] = genres # Thay đổi khóa thành "genres" để dùng trong main()
    info["genre"] = " - ".join(genres) if genres else "N/A"

    # nếu 6 node li thì status ở node[4]; nếu 7 thì ở node[5]
    if len(li_nodes) >= 6:
        idx = 4 if len(li_nodes) == 6 else (5 if len(li_nodes) >= 7 else None)
        info["status"] = _text(li_nodes[idx].find("span")) if idx is not None else "N/A"
    else:
        info["status"] = "N/A"

    return info


def _get_cover_link(soup: BeautifulSoup) -> str:
    """Lấy link cover từ trang chính của truyện."""
    pic = soup.find("div", class_="book-info-pic")
    img = pic.find("img") if pic else None
    return urljoin(soup.base_url, img["src"]) if img and img.has_attr("src") else ""


def _extract_bookid_and_lastpage(paging_div: Optional[BeautifulSoup]) -> Optional[Dict[str, int]]:
    """
    Từ khối phân trang, trích bookid và last_page.
    Ưu tiên nút 'Cuối »' nếu có; nếu không, lấy max số ở các nút có onclick='page(id,n)'.
    """
    if not paging_div:
        return None

    # Tất cả thẻ a có onclick=page(BOOKID, N)
    a_onclick = paging_div.select('a[onclick^="page("]')
    if not a_onclick:
        return None

    # Nếu có nút cuối chứa <span class="arrow">»</span> thì dùng luôn giá trị của nó
    last_arrow = None
    for a in a_onclick:
        if a.find("span", class_="arrow"):
            last_arrow = a
            break

    target = last_arrow if last_arrow else a_onclick[-1]
    m = re.search(r'page\((\d+),\s*(\d+)\);', target.get("onclick", ""))
    if not m:
        return None

    bookid = int(m.group(1))
    # nếu không phải nút cuối, vẫn nên tìm max n trong toàn bộ
    pages = [int(re.search(r'page\(\d+,\s*(\d+)\);', x.get("onclick", "")).group(1))
             for x in a_onclick if re.search(r'page\(\d+,\s*(\d+)\);', x.get("onclick", ""))]
    last_page = max(pages) if pages else int(m.group(2))

    return {"bookid": bookid, "last_page": last_page}


def _get_list_chapters(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """Lấy danh sách chương từ trang chính của truyện (tự xử lý cả khi có/không có phân trang)."""
    chapters: List[Dict[str, str]] = []

    # 1) Không phân trang: Lấy danh sách chương TRỰC TIẾP TỪ UL.list-chapters
    chapter_list_ul = soup.find("div", id="chapter-list")
    paging_div = soup.find("div", class_="paging")

    if chapter_list_ul and (not paging_div or not paging_div.find_all("a")):
        for li in chapter_list_ul.find_all("li"):
            a = li.find("a")
            # Bổ sung kiểm tra href và nội dung để loại bỏ các link không phải chương
            if not a or not a.has_attr("href") or not a.get_text(strip=True):
                continue
            chapters.append({
                "title": _text(a),
                "url": urljoin(soup.base_url, a["href"])
            })
        return chapters

    # 2) Có phân trang: dùng API JSON /get/listchap/{bookid}?page=N
    params = _extract_bookid_and_lastpage(paging_div)
    if not params:
        # Nếu không có list-chapters (hoặc không lấy được) và không có phân trang
        return chapters

    bookid = params["bookid"]
    last_page = params["last_page"]

    for i in range(1, last_page + 1):
        api_url = f"https://metruyenchu.com.vn/get/listchap/{bookid}?page={i}"
        print(f"  > Đang tải trang {i}/{last_page}...")
        page_soup = _fetch_api_fragment(api_url)  # API trả về HTML fragment chứa <li>

        # Tìm tất cả thẻ <li> bên trong fragment
        list_items = page_soup.find_all("li")
        if not list_items:
            time.sleep(SLEEP_BETWEEN_PAGES)
            continue

        for li in list_items:
            a = li.find("a")
            # Bổ sung kiểm tra href và nội dung để loại bỏ các link không phải chương
            if not a or not a.has_attr("href") or not a.get_text(strip=True):
                continue
            chapters.append({
                "title": _text(a),
                "url": urljoin(page_soup.base_url, a["href"]) 
            })

        time.sleep(SLEEP_BETWEEN_PAGES)

    return chapters


def _get_content_of_chapter(chap_url: str) -> BeautifulSoup:
    """Lấy nội dung chương từ URL chương."""
    soup = _fetch_html(chap_url)
    content_div = soup.find("div", class_="truyen")
    return content_div if content_div else BeautifulSoup("", "html.parser")

# =================== MỚI THÊM: HÀM TẢI CHƯƠNG/CHUYỂN ĐỔI ===================

def fetch_chapter_content(chap_url: str) -> Dict[str, str]:
    """Tải nội dung chương và trả về title/content_html/content_txt."""
    print(f"  > Tải: {chap_url}")
    soup = _fetch_html(chap_url)
    # Lấy title từ thẻ h1 class="chapter-title"
    title_node = soup.find("h1", class_="chapter-title")
    content_div = soup.find("div", class_="truyen")

    title = _text(title_node)
    
    # Loại bỏ các thẻ không mong muốn trong nội dung truyện
    if content_div:
        # Xóa các thẻ script/style
        for script_or_style in content_div(["script", "style"]):
            script_or_style.decompose()
        # Xóa thẻ span class="tip" (thường là link donate, quảng cáo)
        for tip in content_div.find_all("span", class_="tip"):
            tip.decompose()
        # Xóa thẻ div class="fb-comments"
        for fb_comment in content_div.find_all("div", class_="fb-comments"):
            fb_comment.decompose()
        
        # Thêm <h1> cho HTML nếu chưa có (không cần thiết vì EPUB Builder sẽ thêm)
        # Bỏ qua phần này để content_html gọn hơn, chỉ giữ nội dung cốt lõi

    content_html = str(content_div) if content_div else ""
    content_txt = _text(content_div) if content_div else "Nội dung chương rỗng."
    
    # Chuẩn hóa lại content_txt: loại bỏ khoảng trắng dư thừa, ký tự đặc biệt nếu có
    # (dù _text đã làm khá tốt)
    content_txt = re.sub(r'\s{2,}', ' ', content_txt).strip()
    
    return {
        "title": title,
        "content_html": content_html,
        "content_txt": content_txt
    }

def save_all_chapters_to_html(title: str, chapters: List[Dict[str, str]], out_dir: str, start: int, end: Optional[int]) -> List[str]:
    """Tải và lưu danh sách chương dưới dạng file HTML."""
    saved_htmls: List[str] = []
    
    chapters_to_process = chapters[(start - 1):(end if end is not None else len(chapters))]
    
    for idx, info in enumerate(chapters_to_process, start):
        try:
            # Kiểm tra URL có vẻ là URL chương không (chứa /chuong-...)
            is_valid_chapter_url = re.search(r'/chuong-\d+', info["url"], re.IGNORECASE) is not None
            
            # Nếu URL không hợp lệ và tiêu đề rỗng thì bỏ qua
            if not is_valid_chapter_url and not info.get("title", "").strip():
                 print(f"⚠️ Bỏ qua URL không phải chương: {info['url']}")
                 continue

            c = fetch_chapter_content(info["url"])
            chap_title = c.get("title") or info.get("title") or f"Chương {idx}"
            
            # Tên file: <thứ tự>-<title slug>.html
            fn = f"{idx:04d}-{_slugify_vi(chap_title)}.html"
            out_path = os.path.join(out_dir, fn)
            
            # Thêm header HTML đầy đủ
            html_content = (
                '<!DOCTYPE html>\n'
                f'<html lang="vi">\n'
                f'<head>\n'
                f'  <meta charset="utf-8"/>\n'
                f'  <title>{html.escape(chap_title)}</title>\n'
                f'</head>\n'
                f'<body>\n'
                f'  <article>\n'
                # Thêm H1 thủ công vào đây nếu content_html không có
                f'    <h1>{html.escape(chap_title)}</h1>\n'
                f'    {c["content_html"]}\n'
                f'  </article>\n'
                f'</body>\n'
                f'</html>'
            )
            
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html_content)
                
            saved_htmls.append(out_path)
            print(f"  > Lưu HTML: {fn}")
        except Exception as e:
            print(f"❌ Lỗi khi tải/lưu chương {idx} ({info.get('url')}): {e}")

        time.sleep(SLEEP_BETWEEN_CHAPS)
        
    return saved_htmls

def save_all_chapters_to_txt(title: str, chapters: List[Dict[str, str]], out_dir: str, start: int, end: Optional[int]) -> List[str]:
    """Tải và lưu danh sách chương dưới dạng file TXT."""
    saved_txts: List[str] = []
    
    chapters_to_process = chapters[(start - 1):(end if end is not None else len(chapters))]
    
    for idx, info in enumerate(chapters_to_process, start):
        try:
            c = fetch_chapter_content(info["url"])
            chap_title = c.get("title") or info.get("title") or f"Chương {idx}"
            
            # Tên file: <thứ tự>-<title slug>.txt
            fn = f"{idx:04d}-{_slugify_vi(chap_title)}.txt"
            out_path = os.path.join(out_dir, fn)
            
            txt_content = f"--- {chap_title.upper()} ---\n\n{c['content_txt']}\n\n"
            
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(txt_content)
                
            saved_txts.append(out_path)
            print(f"  > Lưu TXT: {fn}")
        except Exception as e:
            print(f"❌ Lỗi khi tải/lưu chương {idx} ({info.get('url')}): {e}")
            
        time.sleep(SLEEP_BETWEEN_CHAPS)

    return saved_txts

def convert_htmls_to_txts(out_dir: str, only_paths: List[str]) -> List[str]:
    """Chuyển đổi danh sách file HTML đã tải sang TXT."""
    saved_txts: List[str] = []
    
    for html_path in only_paths:
        try:
            print(f"  > Convert: {os.path.basename(html_path)}")
            with open(html_path, "r", encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), "html.parser")
                
            title_node = soup.find("h1") or soup.find("title")
            title = _text(title_node) or os.path.splitext(os.path.basename(html_path))[0]
            
            # Chỉ lấy nội dung từ thẻ <article> hoặc <body>
            article = soup.find("article") or soup.body
            
            # Dọn dẹp nội dung trước khi chuyển sang TXT
            if article:
                for script_or_style in article(["script", "style"]):
                    script_or_style.decompose()
            
            content_txt = _text(article) if article else "Nội dung chương rỗng."
            content_txt = re.sub(r'\s{2,}', ' ', content_txt).strip()
            
            # Tên file TXT (thay đổi đuôi)
            txt_fn = os.path.splitext(os.path.basename(html_path))[0] + ".txt"
            out_path = os.path.join(out_dir, txt_fn)
            
            txt_content = f"--- {title.upper()} ---\n\n{content_txt}\n\n"
            
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(txt_content)
            
            saved_txts.append(out_path)
        except Exception as e:
            print(f"❌ Lỗi khi convert HTML '{html_path}' sang TXT: {e}")
            
    return saved_txts

# =================== COVER (LOAD & CONVERT) ===================
def _sniff_image_type(data: bytes):
    if not data or len(data) < 12:
        return (".bin", "application/octet-stream")
    if data[:3] == b"\xff\xd8\xff":
        return (".jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return (".png", "image/png")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return (".webp", "image/webp")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return (".gif", "image/gif")
    return (".bin", "application/octet-stream")

def _ensure_jpeg_cover(img_bytes: bytes):
    if not HAS_PILLOW:
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)
    try:
        import io as _io
        im = Image.open(_io.BytesIO(img_bytes))
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, Image.LANCZOS)
            print(f"✔ Cover resized to: {im.size[0]}x{im.size[1]}")
        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        out = _io.BytesIO()
        im.save(out, format="JPEG", quality=90, optimize=True)
        return (out.getvalue(), ".jpg", "image/jpeg")
    except Exception as e:
        ext, mime = _sniff_image_type(img_bytes)
        print(f"⚠ Không convert cover sang JPEG: {e}. Dùng ảnh gốc {ext}.")
        return (img_bytes, ext, mime)

def _fetch_cover_from_book_page(book_page_url: str):
    soup = _fetch_html(book_page_url)
    src = _get_cover_link(soup)
    if src:
        src = urljoin(book_page_url, src)
        r = requests.get(src, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content
    return None

def _load_cover_from_input(cover_in: str, story_url: Optional[str]):
    data = None
    if cover_in:
        try:
            if cover_in.lower().startswith(("http://", "https://")):
                print(f"Tải cover từ URL: {cover_in}")
                r = requests.get(cover_in, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.content
            else:
                print(f"Đọc cover từ file: {cover_in}")
                with open(cover_in, "rb") as f:
                    data = f.read()
        except Exception as e:
            print(f"⚠ Không tải/đọc cover '{cover_in}': {e}")
    if data is None and story_url:
        try:
            print("Tự lấy cover từ trang truyện…")
            cb = _fetch_cover_from_book_page(story_url)
            if cb:
                data = cb
        except Exception as e:
            print(f"⚠ Không lấy được cover từ web: {e}")
    if data:
        return _ensure_jpeg_cover(data)
    return (None, None, None)

# =================== EPUB BUILDER (EPUB2/EPUB3) ===================
def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)

def create_epub(book_url: Optional[str],
                book_title: str,
                author: str,
                chapters,                  # list[str path_html] hoặc list[dict{url,title?}]
                out_epub_path: str,        # file .epub hoặc thư mục
                creator: str = "Hishiro",
                language: str = "vi",
                epub_target: Optional[str] = None,
                cover_bytes: Optional[bytes] = None,
                cover_ext: Optional[str] = None,
                cover_mime: Optional[str] = None) -> str:

    book_title = book_title or "Truyện"
    author     = author or "—"
    target     = (epub_target or EPUB_TARGET).lower().strip()

    out_is_dir = (os.path.isdir(out_epub_path) or not out_epub_path.lower().endswith(".epub"))
    if out_is_dir:
        os.makedirs(out_epub_path, exist_ok=True)
        out_file = os.path.join(out_epub_path, f"{_slugify_vi(book_title)}.epub")
    else:
        os.makedirs(os.path.dirname(out_epub_path) or ".", exist_ok=True)
        out_file = out_epub_path

    # Thu thập nội dung
    items = []
    if chapters and isinstance(chapters[0], str):
        # Đọc HTML cục bộ
        for i, html_path in enumerate(chapters, 1):
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    soup = BeautifulSoup(f.read(), "html.parser")
                
                # Title ưu tiên <h1> trong article, sau đó là <title>
                article = soup.select_one("article")
                title_node = article.find("h1") if article else None
                if not title_node:
                     title_node = soup.find("title")

                if title_node:
                    title = title_node.get_text(strip=True)
                else:
                    base = os.path.splitext(os.path.basename(html_path))[0]
                    m = re.match(r"^\s*\d+\s*-\s*(.+)$", base)
                    title = m.group(1).strip() if m else base
                
                node = article or soup.body or soup
                
                # Gỡ h1 để nó không bị lặp khi EPUB builder tự thêm h1
                # Nếu có h1 trong node, gỡ nó đi
                content_html_soup = BeautifulSoup(str(node), 'html.parser')
                h1_tag = content_html_soup.find('h1')
                if h1_tag:
                    h1_tag.decompose()
                
                content_html = "".join(str(x) for x in content_html_soup.children)
                items.append({"title": title, "content_html": content_html})
            except Exception as e:
                print(f"WARN đọc HTML '{html_path}': {e}")
    else:
        # Fetch từ web
        for idx, info in enumerate(chapters or [], 1):
            # Kiểm tra URL có vẻ là URL chương không
            is_valid_chapter_url = re.search(r'/chuong-\d+', info["url"], re.IGNORECASE) is not None
            if not is_valid_chapter_url and not info.get("title", "").strip():
                 print(f"⚠️ Bỏ qua URL không phải chương: {info['url']}")
                 continue
                 
            c = fetch_chapter_content(info["url"])
            if not c.get("title"):
                c["title"] = info.get("title") or f"Chương {idx}"
            
            # Gỡ <h1> từ content_html vì nó sẽ được thêm lại trong template EPUB
            content_soup = BeautifulSoup(c["content_html"], 'html.parser')
            h1_tag = content_soup.find('h1')
            if h1_tag:
                h1_tag.decompose()
            c["content_html"] = str(content_soup)
            
            items.append(c)
            time.sleep(SLEEP_BETWEEN_CHAPS)

    # Cover fallback nếu thiếu
    if cover_bytes is None and book_url:
        try:
            print("Fallback cover (trong create_epub)…")
            cb = _fetch_cover_from_book_page(book_url)
            if cb:
                cover_bytes, cover_ext, cover_mime = _ensure_jpeg_cover(cb)
        except Exception:
            cover_bytes, cover_ext, cover_mime = None, None, None

    if cover_bytes is not None and (not cover_ext or not cover_mime):
        cover_ext, cover_mime = _sniff_image_type(cover_bytes)

    cover_relpath = f"Images/cover{cover_ext}" if (cover_bytes and cover_ext) else None

    with zipfile.ZipFile(out_file, "w") as z:
        # mimetype
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        # container.xml
        container_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n'
            '</container>'
        ).encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)

        # cover.xhtml (cho cả epub2/3)
        if cover_relpath:
            cover_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi">\n'
                '<head>\n'
                '  <title>Cover</title>\n'
                '  <meta charset="utf-8"/>\n'
                '  <style type="text/css">.cover{height:100vh;max-width:100%;object-fit:contain;margin:0 auto;display:block}</style>\n'
                '</head>\n'
                '<body>\n'
                f'  <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="100%" height="100%" viewBox="0 0 1000 1500" preserveAspectRatio="xMidYMid meet">\n'
                f'    <image width="1000" height="1500" xlink:href="../{cover_relpath}" class="cover"/>\n'
                f'  </svg>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/Text/cover.xhtml", cover_xhtml)

        # CSS
        style_css = (
            "body{font-family:serif;line-height:1.6}"
            "img{max-width:100%;height:auto}"
            "h1{font-size:1.4em;margin:0 0 .6em}"
        )
        _epub_write(z, "OEBPS/Styles/style.css", style_css.encode("utf-8"))

        # Chương
        manifest_items, spine_items, navpoints = [], [], []
        chap_refs = []

        if cover_relpath:
            manifest_items.append('<item id="cover-html" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="cover-html" linear="no"/>')

        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            chapter_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">\n'
                '<head>\n'
                f'  <title>{html.escape(c["title"])}</title>\n'
                '  <link href="../Styles/style.css" rel="stylesheet" type="text/css"/>\n'
                '  <meta charset="utf-8"/>\n'
                '</head>\n'
                '<body>\n'
                f'  <h1>{html.escape(c["title"])}</h1>\n' # Title hiển thị trong chương
                f'  <article>\n'
                f'    {c["content_html"]}\n'
                f'  </article>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, f"OEBPS/{fn}", chapter_xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            navpoints.append(
                f'<navPoint id="navPoint-{i}" playOrder="{i}" class="chapter">'
                f'<navLabel><text>{html.escape(c["title"])}</text></navLabel>'
                f'<content src="{fn}"/></navPoint>'
            )
            chap_refs.append((fn, c["title"]))

        # Cover image
        if cover_relpath and cover_bytes:
            _epub_write(z, f"OEBPS/{cover_relpath}", cover_bytes)

        manifest_items_str = "\n    ".join(manifest_items)
        spine_items_str    = "\n    ".join(spine_items)
        navpoints_str      = "\n    ".join(navpoints)

        dt_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cover_media_item = f'<item id="cover-image" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}" properties="cover-image"/>' if cover_relpath else "" # Thêm properties="cover-image" cho EPUB3

        if target == "epub3":
            # nav.xhtml
            nav_list_items = "\n".join(f'      <li><a href="{fn}">{html.escape(t)}</a></li>' for fn, t in chap_refs)
            nav_html = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" xmlns:epub="http://www.idpf.org/2007/ops">\n'
                '<head>\n'
                '  <meta charset="utf-8"/>\n'
                '  <title>Table of Contents</title>\n'
                '  <link href="Styles/style.css" rel="stylesheet" type="text/css"/>\n'
                '</head>\n'
                '<body>\n'
                '  <nav epub:type="toc" id="toc">\n'
                f'    <h1>{html.escape(book_title)}</h1>\n'
                '    <ol>\n'
                f'{nav_list_items}\n'
                '    </ol>\n'
                '  </nav>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/nav.xhtml", nav_html)

            content_opf = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="3.0">\n'
                '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
                f'    <dc:identifier id="BookID">urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}</dc:identifier>\n'
                f'    <dc:title>{html.escape(book_title)}</dc:title>\n'
                f'    <dc:creator>{html.escape(author)}</dc:creator>\n'
                f'    <dc:language>{language}</dc:language>\n'
                f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
                f'    <meta name="creator" content="{html.escape(creator)}"/>\n'
                f'    <dc:publisher>{html.escape(creator)}</dc:publisher>\n'
                f'    <meta property="cover" content="cover-image"/>\n' # Thêm meta cover cho EPUB3
                '  </metadata>\n'
                '  <manifest>\n'
                f'    {cover_media_item}\n'
                '    <item id="css" href="Styles/style.css" media-type="text/css"/>\n'
                '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
                f'    {manifest_items_str}\n'
                '  </manifest>\n'
                '  <spine>\n'
                f'    {spine_items_str}\n'
                '  </spine>\n'
                '</package>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/content.opf", content_opf)

        else:
            # EPUB2: toc.ncx + opf 2.0
            if cover_relpath:
                manifest_cover = f'<item id="cover-image" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}"/>'
                guide_ref = f'<reference type="cover" title="Cover" href="Text/cover.xhtml"/>'
            else:
                manifest_cover = ""
                guide_ref = ""

            content_opf = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="2.0">\n'
                '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
                f'    <dc:identifier id="BookID">urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}</dc:identifier>\n'
                f'    <dc:title>{html.escape(book_title)}</dc:title>\n'
                f'    <dc:creator>{html.escape(author)}</dc:creator>\n'
                f'    <dc:publisher>{html.escape(creator)}</dc:publisher>\n'
                f'    <dc:language>{language}</dc:language>\n'
                '    <meta name="cover" content="cover-image"/>\n'
                f'    <meta name="creator" content="{html.escape(creator)}"/>\n'
                f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
                '  </metadata>\n'
                '  <manifest>\n'
                f'    {cover_media_item}\n'
                f'    {manifest_cover}\n'
                '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
                '    <item id="css" href="Styles/style.css" media-type="text/css"/>\n'
                f'    {manifest_items_str}\n'
                '  </manifest>\n'
                '  <spine toc="ncx">\n'
                f'    {spine_items_str}\n'
                '  </spine>\n'
                '  <guide>\n'
                f'    {guide_ref}\n'
                '  </guide>\n'
                '</package>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/content.opf", content_opf)

            toc_ncx = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"\n'
                '  "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
                '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
                '  <head>\n'
                f'    <meta name="dtb:uid" content="urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}"/>\n' # Thêm time.time() để uid duy nhất
                '    <meta name="dtb:depth" content="1"/>\n'
                '    <meta name="dtb:totalPageCount" content="0"/>\n'
                '    <meta name="dtb:maxPageNumber" content="0"/>\n'
                '  </head>\n'
                f'  <docTitle><text>{html.escape(book_title)}</text></docTitle>\n'
                '  <navMap>\n'
                f'    {navpoints_str}\n'
                '  </navMap>\n'
                '</ncx>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/toc.ncx", toc_ncx)

    return out_file

def build_epub(epub_out_dir: str, title: str, author: str, html_paths: List[str],
               cover_bytes=None, cover_ext=None, cover_mime=None, epub_target: Optional[str] = None) -> str:
    paths = [p for p in (html_paths or []) if isinstance(p, str) and p.lower().endswith(".html") and os.path.isfile(p)]
    if not paths:
        raise ValueError("build_epub: Không có file HTML hợp lệ để đóng EPUB.")

    def _sort_key(p: str):
        base = os.path.splitext(os.path.basename(p))[0]
        m = re.match(r"^\s*(\d+)", base)
        idx = int(m.group(1)) if m else 10**9
        return (idx, base.casefold())
    paths.sort(key=_sort_key)

    return create_epub(
        book_url=None,
        book_title=title,
        author=author,
        chapters=paths,
        out_epub_path=epub_out_dir,
        creator="Hishiro",
        language="vi",
        epub_target=epub_target or EPUB_TARGET,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime
    )

def _delete_files(paths: List[str]):
    ok = 0
    for p in paths or []:
        try:
            if os.path.isfile(p):
                os.remove(p)
                ok += 1
        except Exception as e:
            print(f"⚠ Không xóa được {p}: {e}")
    print(f"🧹 Đã xóa {ok}/{len(paths or [])} file")

def main():
    StoryUrl = input("Nhập URL: ").strip()
    # Kiểm tra URL đầu vào
    if not StoryUrl.startswith(("http://", "https://")):
        StoryUrl = "https://" + StoryUrl

    cover_in = input("Nhập đường dẫn Cover (bỏ trống để tự lấy từ web/URL): ").strip()
    
    # Lấy BeautifulSoup cho trang chính một lần
    try:
        main_soup = _fetch_html(StoryUrl)
    except requests.exceptions.HTTPError as e:
        print(f"❌ Lỗi tải trang chính: {e}")
        return
    except requests.exceptions.RequestException as e:
        print(f"❌ Lỗi kết nối: {e}")
        return

    
    info = _get_book_info(main_soup)
    title    = info.get("title") or "Truyện" # Dùng key "title"
    author   = info.get("author") or "—"    # Dùng key "author"
    
    
    # Thay đổi: truyền main_soup vào _get_list_chapters
    chapters = _get_list_chapters(main_soup)

    print("----------------STORY INFO----------------")
    print("Title :", title)
    print("Author:", author)
    print("Genre :", ", ".join(info.get("genres", [])) or "—") # Dùng key "genres"
    print("Status:", info.get("status", "—")) # Dùng key "status"
    print("Total Chapters:", len(chapters))
    print("------------------------------------------")
    
    if not chapters:
        print("❌ Không tìm thấy chương nào. Kết thúc.")
        return

    print("\nChọn chức năng:")
    print("[1]: Tải và lưu dạng HTML")
    print("[2]: Tải và lưu dạng TXT (Chuyển đổi từ HTML, sau đó xóa HTML)")
    print("[3]: Tải và lưu dạng HTML + TXT")
    print("[4]: Tải và lưu dạng HTML + Build Epub")
    print("[5]: Tải và lưu dạng TXT + Build Epub")
    print("[6]: Tải và lưu dạng HTML + TXT + Build Epub")
    choice = input("Nhập lựa chọn (1-6): ").strip()
    
    # Hỏi target EPUB nếu có build (4/5/6)
    do_epub = choice in {"4", "5", "6"}
    if do_epub:
        global EPUB_TARGET
        target_in = input(f"Chọn EPUB target (2=EPUB 2, 3=EPUB 3 compat) [{EPUB_TARGET[4]}]: ").strip()
        
        EPUB_TARGET = "epub3" if target_in == "3" else "epub2"
        print(f"EPUB target: {EPUB_TARGET}")
        if not HAS_PILLOW and EPUB_TARGET in ["epub2", "epub3"]:
             print("⚠️ EPUB cover có thể không hiển thị tối ưu trên Kobo nếu thiếu Pillow (JPEG/Resize).")

    # Chuẩn bị cover (local/URL/auto) — luôn ưu tiên JPEG/Resize
    print("\n--- 3. Handling Cover Image ---")
    cover_bytes, cover_ext, cover_mime = _load_cover_from_input(cover_in, StoryUrl)
    print(f"Cover status: {'OK' if cover_bytes else 'MISSING'} ({cover_mime})")

    out_dir = os.path.join("output", _slugify_vi(title)) # Dùng slugified title
    epub_out_dir = "output"
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(epub_out_dir, exist_ok=True)
    print("Output dir:", out_dir)

    saved_htmls: list[str] = []
    saved_txts:  list[str] = []
    epub_path:   str | None = None

    if choice in {"1", "3", "4", "6"}: # Tải HTML (1, 3, 4, 6)
        print(f"\n[Mode {choice}] Tải HTML…")
        # Thay đổi: start=1, end=None là mặc định tải hết
        saved_htmls = save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)
        print(f"✔ Đã lưu HTML: {len(saved_htmls)} files.")
    
    if choice == "2": # Mode 2: Tải TXT trực tiếp
        print(f"\n[Mode 2] Tải và lưu TXT…")
        saved_txts = save_all_chapters_to_txt(title, chapters, out_dir, start=1, end=None)
        print(f"✔ Đã lưu TXT: {len(saved_txts)} files.")
            
    elif choice in {"3", "5", "6"}: # Các mode cần TXT trung gian hoặc song song
        if not saved_htmls and choice == "5": # Mode 5: Tải HTML trung gian để convert (vì cần HTML để build EPUB)
            print("⚠️ Cần phải có file HTML để build EPUB. Bắt đầu tải HTML trung gian...")
            saved_htmls = save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)
            
        if saved_htmls:
            print(f"\n[Mode {choice}] Trích TXT từ HTML đã tải…")
            saved_txts = convert_htmls_to_txts(out_dir, only_paths=saved_htmls)
            print(f"✔ Đã lưu TXT: {len(saved_txts)} files.")
        elif choice != "5":
            print("❌ Không có HTML để trích TXT.")

    if choice in {"4", "5", "6"}:
        print(f"\n[Mode {choice}] Build EPUB...")
        # Check lại: nếu chọn build EPUB mà HTML chưa được tải (chỉ xảy ra khi mode 5)
        if not saved_htmls:
            print("⚠️ Cần phải có file HTML để build EPUB. Bắt đầu tải HTML lại...")
            saved_htmls = save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)
            
        if saved_htmls:
            try:
                epub_path = build_epub(epub_out_dir, title, author, saved_htmls,
                                    cover_bytes=cover_bytes, cover_ext=cover_ext, cover_mime=cover_mime,
                                    epub_target=EPUB_TARGET)
                print(f"✔ EPUB: {epub_path}")
            except Exception as e:
                print(f"❌ Lỗi khi build EPUB: {e}")
                epub_path = None
        else:
             print("❌ Không có HTML để build EPUB.")

    # Xóa file HTML nếu người dùng chỉ chọn TXT (Mode 2) hoặc TXT+EPUB (Mode 5)
    if choice in {"2", "5"}:
        if saved_htmls:
            print(f"\n🧹 Xóa file HTML trung gian...")
            _delete_files(saved_htmls)
            saved_htmls = []
        
    print("\n------------------- DONE -------------------")
    if epub_path:
        print(f"✅ Đã tạo EPUB: {epub_path}")
    if saved_htmls:
        print(f"✅ Đã lưu HTML tại: {out_dir}")
    if saved_txts:
        print(f"✅ Đã lưu TXT tại: {out_dir}")
    print("--------------------------------------------")

if __name__ == "__main__":
    main()