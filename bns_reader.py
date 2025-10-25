# -*- coding: utf-8 -*-
'''
    @Author: Hishio - Cá mụp cắn cáp
    @github: https://github.com/hishirooo
    @date: 19-10-2025
    @version: 1.0
    bns_reader.py - Tải truyện và tạo EPUB (https://bnsach.com/reader)
    Chế độ :
        1. Tài HTML
        2. Tài TXT
        3. Tài HTML + Tạo EPUB (đọc lại HTML có sẵn)
        4. Tài TXT  + Tạo EPUB (TXT -> bọc XHTML tối giản -> EPUB)
        5. Tài HTML + TXT + Tạo EPUB (EPUB dùng HTML có sẵn)
    Cover:
        - Sau khi nhập URL, nhập đường dẫn cover (file local hoặc URL ảnh).
        - Bỏ trONGL sẽ tự lý cover từ DOM: div.book-img img[src]
        - Nếu ảnh khóa hình → convert 1 lần sang JPEG (cần Pillow).
    Log:
        - Thư mục đầu ra: ./Output/<TieuDeTruyen> (không dấu hoặc có dấu tùy cấu hình)
        - EPUB sẽ lưu ở: ./Output/<TieuDeTruyen>.epub (không nằm trong thư mục truyện)
        - Khi tải:   Saved : 0001.xhtml - TieuDeChuong
        - Khi tạo:   Readfile : 0001.xhtml from Output/<TieuDeTruyen>

'''


import os, time, requests, unicodedata, re, html, glob, zipfile, datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
import io


HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}
EPUB_TARGET = "epub3"

# Thử import Pillow cho xử lý ảnh bìa
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("⚠ Cảnh báo: Không tìm thấy thư viện Pillow. Không thể tối ưu/chuyển đổi cover sang JPEG.")


base_dir = os.path.dirname(os.path.abspath(__file__))
driver_path = os.path.join(base_dir, "chromedriver-win64", "chromedriver.exe")

def login_and_get_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    service = Service(driver_path)
    driver = webdriver.Chrome(service=service, options=options)

    driver.get("https://bnsach.com/forum/login")
    driver.find_element(By.NAME, "login").send_keys("Hishiro000")
    driver.find_element(By.NAME, "password").send_keys("821990Miku")
    driver.find_element(By.NAME, "password").send_keys(Keys.RETURN)
    time.sleep(3)
    return driver

def get_cookies_from_selenium() -> Dict[str, str]:
    with login_and_get_driver() as driver:
        driver.get("https://bnsach.com/reader")
        time.sleep(2)
        cookies = driver.get_cookies()
    return {cookie['name']: cookie['value'] for cookie in cookies}

def fetch_reader_with_requests(url: str, cookies: Dict[str, str]) -> requests.Response:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://bnsach.com/forum/",
        "Accept": "text/html",
        "Accept-Language": "vi,en-US;q=0.9",
        "Upgrade-Insecure-Requests": "1"
    }
    return requests.get(url, headers=headers, cookies=cookies)

def _get_book_info(response: requests.Response) -> Dict[str, str]:
    if "Đăng nhập để đọc truyện" in response.text:
        print("❌ Truy cập Reader vẫn bị chặn.")
        return {}
    print("✅ Truy cập Reader thành công!")
    soup = BeautifulSoup(response.text, "html.parser")
    return {
        "title": soup.select_one("h1#truyen-title").text.strip(),
        "author": soup.select_one("div#tacgia a").text.strip(),
        "genre": soup.select_one("div#theloai a").text.strip(),
        "status": soup.select("div#flag span.flag-term")[0].text.strip(),
        "cover": soup.select_one("div#anhbia img")["src"]
    }

def _get_list_chapters(response: requests.Response) -> List[Dict[str, str]]:
    soup = BeautifulSoup(response.text, "html.parser")
    chapters = []
    for li in soup.select("li.chuong-item"):
        a_tag = li.select_one("a.chuong-link")
        if a_tag:
            title = a_tag.select_one("span.chuong-name").text.strip()
            link = "https://bnsach.com" + a_tag["href"]
            chapters.append({"title": title, "link": link})
    return chapters

def _get_content_chapter(url: str) -> str:
    with login_and_get_driver() as driver:
        driver.get(url)
        time.sleep(3)
        driver.execute_script("""
            let scripts = document.querySelectorAll('script[src*="content-protector.js"]');
            scripts.forEach(s => s.remove());
        """)
        time.sleep(2)
        html = driver.page_source

    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("div#noi-dung")
    return content.prettify() if content else ""


def save_chapter_html(index: int, total: int, title: str, content_html: str):
    # Làm sạch tiêu đề để tránh lỗi tên file
    safe_title = title.replace(":", "").replace("?", "").replace("/", "").strip()
    
    # Tạo tên file theo định dạng xxxx.html
    filename = f"chapter_{index:04d}.html"
    
    # Tạo nội dung HTML đầy đủ
    html_template = f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <title>{safe_title}</title>
</head>
<body>
    <h1>{title}</h1>
    <div>{content_html}</div>
</body>
</html>"""

    # Ghi ra file
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_template)

    # Xuất log ra terminal
    print(f"[{index:04d}/{total:04d}] Saved - {filename} - {title}")

def save_all_chapters_to_html(
    chapters: List[Dict[str, str]],
    story_title: str,
    start: int = 1,
    end: Optional[int] = None
):
    total = len(chapters)
    end = end if end else total
    start = max(1, start)
    end = min(end, total)

    # Tạo thư mục lưu file
    folder_name = f"output/{story_title.strip().replace(' ', '_')}"
    os.makedirs(folder_name, exist_ok=True)

    for index in range(start, end + 1):
        chap = chapters[index - 1]
        title = chap["title"]
        url = chap["link"]

        #print(f"📥 Đang tải chương {index}/{total}: {title}")
        content_html = _get_content_chapter(url)

        if content_html:
            filename = f"{folder_name}/chapter_{index:04d}.html"
            html_template = f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
</head>
<body>
    <h1>{title}</h1>
    <div>{content_html}</div>
</body>
</html>"""
            with open(filename, "w", encoding="utf-8") as f:
                f.write(html_template)
            print(f"[{index:04d}/{total:04d}] 📥Saved - chapter_{index:04d}.html - {title}")
        else:
            print(f"[{index:04d}/{total:04d}] ❌ Không thể lấy nội dung - {title}")

def _slugify_vi(s: str) -> str:
    """Slug ASCII an toàn cho tên file (Windows/Kobo thân thiện)."""
    s = (s or "book").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-_")
    return (s or "book")[:80]

def normalize_chapter_title(t: Optional[str]) -> Optional[str]:
    """Sửa 'Chương10:...' -> 'Chương 10: ...'."""
    if not t: return t
    t = t.strip()
    m = re.match(r"^(Chương)\s*(\d+)(\s*[:\-–]?\s*)(.*)$", t, flags=re.I)
    if m:
        name, num, _, rest = m.groups()
        rest = rest.strip()
        return f"{name} {num}: {rest}" if rest else f"{name} {num}"
    return t

def _sniff_image_type(data: bytes) -> tuple[str, str]:
    """
    Đoán định dạng ảnh từ header.
    Trả về (ext, mime). Nếu không biết: ('.bin', 'application/octet-stream')
    """
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

def _epub_write(zipf, arcname, data_bytes, compress=True):
    '''
    zipf: zipfile.ZipFile
    arcname: str
    data_bytes: bytes
    compress: bool
    '''
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_STORED if not compress else zipfile.ZIP_DEFLATED
    zipf.writestr(zinfo, data_bytes)

def create_epub(book_url: str | None,
                book_title: str,
                author: str,
                chapters: list,            # CHỈ hỗ trợ list[str path_html]
                out_epub_path: str,        # đường dẫn .epub HOẶC THƯ MỤC
                creator: str = "Hishiro",
                language: str = "vi",
                epub_target: str | None = None,
                cover_bytes: bytes | None = None,
                cover_ext: str | None = None,
                cover_mime: str | None = None) -> str:
    """
    Tạo EPUB2 hoặc EPUB3 từ các file HTML cục bộ.
    ĐÃ CHỈNH SỬA để đọc template HTML của bns_reader.py (h1, div).
    """
    book_title = book_title or "Truyện"
    author     = author or "—"
    target     = (epub_target or "epub3").lower().strip()

    # 0) Output path
    out_is_dir = (os.path.isdir(out_epub_path) or not out_epub_path.lower().endswith(".epub"))
    if out_is_dir:
        os.makedirs(out_epub_path, exist_ok=True)
        out_file = os.path.join(out_epub_path, f"{_slugify_vi(book_title)}.epub")
    else:
        os.makedirs(os.path.dirname(out_epub_path) or ".", exist_ok=True)
        out_file = out_epub_path

    # 1) Thu thập nội dung chương (Đã chỉnh sửa cho bns_reader)
    items = []
    if chapters and isinstance(chapters[0], str):
        # Đọc từ HTML cục bộ
        for i, html_path in enumerate(chapters, 1):
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    soup = BeautifulSoup(f.read(), "html.parser")
                
                # Logic đọc template của bns_reader.py
                title_node = soup.select_one("h1")
                if title_node:
                    title = normalize_chapter_title(title_node.get_text(strip=True))
                else:
                    # Fallback: đoán từ tên file
                    base = os.path.splitext(os.path.basename(html_path))[0]
                    m = re.match(r"^\s*\d+\s*-\s*(.+)$", base)
                    title = normalize_chapter_title(m.group(1).strip()) if m else base
                
                # Logic đọc template của bns_reader.py
                node = soup.select_one("body > div") # Lấy div nội dung
                if node:
                    content_html = node.decode_contents() # Lấy HTML bên trong
                else:
                    content_html = soup.body.decode_contents() # Fallback

                items.append({"title": title, "content_html": content_html})
            except Exception as e:
                print(f"WARN đọc HTML '{html_path}': {e}")
    else:
        print("Lỗi: create_epub chỉ hỗ trợ đầu vào là danh sách file HTML.")
        return "" # Hoặc raise lỗi

    # 2) Cover
    if cover_bytes is not None and (not cover_ext or not cover_mime):
        # đoán nếu thiếu
        cover_ext, cover_mime = _sniff_image_type(cover_bytes)

    cover_relpath = f"Images/cover{cover_ext}" if (cover_bytes and cover_ext) else None

    # 3) Đóng EPUB
    with zipfile.ZipFile(out_file, "w") as z:
        # 3.1 mimetype
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        # 3.2 container.xml
        container_xml = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<container version=\"1.0\" xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">\n"
            "  <rootfiles>\n"
            "    <rootfile full-path=\"OEBPS/content.opf\" media-type=\"application/oebps-package+xml\"/>\n"
            "  </rootfiles>\n"
            "</container>"
        ).encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)
        
        # Thêm file cover.xhtml
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


        # 3.3 Styles
        style_css = (
            "body{font-family:serif;line-height:1.6}"
            "img{max-width:100%;height:auto}"
            "h1{font-size:1.4em;margin:0 0 .6em}"
            #/* Thêm style từ bns_reader.py nếu cần */
            "div#noi-dung p { margin-bottom: 1em; }" 
        )
        _epub_write(z, "OEBPS/Styles/style.css", style_css.encode("utf-8"))

        # 3.4 Chapters
        manifest_items, spine_items, navpoints = [], [], []
        chap_refs = []  # (fn, title) phục vụ EPUB3 nav.xhtml
        
        if cover_relpath:
            manifest_items.append('<item id="cover-html" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="cover-html" linear="no"/>')

        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            chapter_xhtml = (
                "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
                "<!DOCTYPE html>\n"
                f"<html xmlns=\"http://www.w3.org/1999/xhtml\" xml:lang=\"{language}\">\n"
                "<head>\n"
                f"  <title>{html.escape(c['title'])}</title>\n"
                "  <link href=\"../Styles/style.css\" rel=\"stylesheet\" type=\"text/css\"/>\n"
                "  <meta charset=\"utf-8\"/>\n"
                "</head>\n"
                "<body>\n"
                f"  <h1>{html.escape(c['title'])}</h1>\n"
                f"  <div>{c['content_html']}</div>\n"
                "</body>\n"
                "</html>"
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

        # 3.5 Cover Media
        if cover_relpath and cover_bytes:
            _epub_write(z, f"OEBPS/{cover_relpath}", cover_bytes)

        # 3.6 OPF + NCX / NAV
        manifest_items_str = "\n    ".join(manifest_items)
        spine_items_str    = "\n    ".join(spine_items)
        navpoints_str      = "\n    ".join(navpoints)
        dt_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        cover_media_item = f'<item id="cover-image" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}"/>' if cover_relpath else ""

        if target == "epub3":
            # EPUB3
            if cover_relpath:
                cover_item = f'<item id="cover-img" href="{cover_relpath}" media-type="{cover_mime or "image/jpeg"}" properties="cover-image"/>'
            else:
                cover_item = ""
            
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
                '    <ol>\n' +
                "\n".join(f'      <li><a href="{fn}">{html.escape(t)}</a></li>' for fn, t in chap_refs) +
                '\n    </ol>\n'
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
                f'    <meta name="creator" content="{html.escape(creator)}"/>\n'
                f'    <dc:publisher>{html.escape(author)}</dc:publisher>\n'
                f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
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
            # EPUB2
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
                f'    <dc:publisher>{html.escape(author)}</dc:publisher>\n'
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
                f'    <meta name="dtb:uid" content="urn:uuid:{_slugify_vi(book_title)}"/>\n'
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

def build_epub(epub_out_dir: str, title: str, author: str, html_paths: list[str],
               cover_bytes: bytes | None = None, cover_ext: str | None = None, cover_mime: str | None = None,
               epub_target: str | None = None) -> str:
    """
    Hàm bọc (wrapper): Nhận list đường dẫn HTML, sắp xếp, rồi gọi create_epub.
    """
    paths = [p for p in (html_paths or []) if isinstance(p, str) and p.lower().endswith(".html") and os.path.isfile(p)]
    if not paths:
        raise ValueError("build_epub: Không có file HTML hợp lệ để đóng EPUB.")

    def _sort_key(p: str):
        '''
        Sắp xếp theo số chương (ví dụ: 'chapter_0001.html')
        '''
        base = os.path.splitext(os.path.basename(p))[0]
        # Chỉnh sửa regex để khớp với 'chapter_0001.html'
        m = re.match(r"^\s*chapter_(\d+)", base, re.IGNORECASE)
        idx = int(m.group(1)) if m else 10**9
        return (idx, base.casefold())
    
    paths.sort(key=_sort_key)

    print(f"Bắt đầu build EPUB với {len(paths)} chương...")
    
    return create_epub(
        book_url=None, # Không cần vì đã xử lý cover trong main()
        book_title=title,
        author=author,
        chapters=paths, # Truyền danh sách file HTML đã sắp xếp
        out_epub_path=epub_out_dir,
        creator="Hishiro",
        language="vi",
        epub_target=epub_target or "epub3",
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime
    )
def main():
    
    story_url = input("Nhập URL:").strip()
    cookies = get_cookies_from_selenium()
    response = fetch_reader_with_requests(story_url, cookies)
    cover_in = input("Nhập đường dẫn Cover (bỏ trống để tự lấy từ web/URL): ").strip()
    
    info = _get_book_info(response)
    print("--------------------------INFO TRUYỆN--------------------------")
    for k, v in info.items():
        print(f"{k.capitalize():<12}: {v}")
    print("------------------------------------------")

    print("\nChọn chức năng:")
    print("[1]: Tải và lưu dạng HTML")
    print("[2]: Tải và lưu dạng TXT (Chuyển đổi từ HTML, sau đó xóa HTML)")
    print("[3]: Tải và lưu dạng HTML + TXT")
    print("[4]: Tải và lưu dạng HTML + Build Epub")
    print("[5]: Tải và lưu dạng TXT + Build Epub")
    print("[6]: Tải và lưu dạng HTML + TXT + Build Epub")
    choice = input("Nhập lựa chọn (1-6): ").strip()
    
    print("---------------------------------------------------------------")

    print("Đang tải danh sách chương...")
    chapters = _get_list_chapters(fetch_reader_with_requests(story_url + "/muc-luc?page=all", cookies))
    print(f"Saved {len(chapters)} link chapter from API.")
    print("Đang tải nội dung chương...")
    save_all_chapters_to_html(chapters, info.get("title", "Unknown_Story"), start=1, end=None)
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

    if choice in {"1", "3", "4", "5", "6"}:
        print(f"\n[Mode {choice}] Tải HTML…")
        saved_htmls = save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)
        print(f"✔ Đã lưu HTML: {len(saved_htmls)} files.")
    
    if choice in {"2", "3", "5", "6"}:
        if choice == "2": # Mode 2: Tải TXT trực tiếp (tối ưu, không qua HTML trung gian nếu không cần EPUB)
            print(f"\n[Mode 2] Tải và lưu TXT…")
            saved_txts = save_all_chapters_to_txt(title, chapters, out_dir, start=1, end=None)
            print(f"✔ Đã lưu TXT: {len(saved_txts)} files.")
            
        elif choice in {"3", "5", "6"}: # Các mode cần HTML/TXT song song hoặc cần HTML để build EPUB/TXT
            print(f"\n[Mode {choice}] Trích TXT từ HTML đã tải…")
            saved_txts = convert_htmls_to_txts(out_dir, only_paths=saved_htmls)
            print(f"✔ Đã lưu TXT: {len(saved_txts)} files.")

    if choice in {"4", "5", "6"}:
        print(f"\n[Mode {choice}] Build EPUB...")
        if not saved_htmls:
            print("⚠️ Cần phải có file HTML để build EPUB. Bắt đầu tải HTML lại...")
            saved_htmls = save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)
            
        if saved_htmls:
            epub_path = build_epub(epub_out_dir, title, author, saved_htmls,
                                cover_bytes=cover_bytes, cover_ext=cover_ext, cover_mime=cover_mime,
                                epub_target=EPUB_TARGET)
            print(f"✔ EPUB: {epub_path}")

    # Xóa file HTML nếu người dùng chỉ chọn TXT (Mode 2) hoặc TXT+EPUB (Mode 5)
    if choice in {"2", "5"}:
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

if __name__ == "__main__":
    main()
