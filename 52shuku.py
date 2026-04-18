import os
import re
import time
import uuid
import zipfile
import requests
from bs4 import BeautifulSoup
from typing import List, Dict

# =============== CẤU HÌNH ===============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
SLEEP_BETWEEN_CHAPS = 0.2
OUTPUT_DIR = "output"

# ================= HELPER =================
def _text(el) -> str:
    '''Lấy text từ thẻ HTML, dọn dẹp khoảng trắng rác.'''
    try:
        if not el: return ""
        text = el.get_text(" ", strip=True)
        text = text.replace('\u3000', ' ').replace('\xa0', ' ')
        return re.sub(r'\s+', ' ', text).strip()
    except Exception:
        return ""

def _fetch_html(url: str) -> BeautifulSoup:
    '''Tải nội dung trang web và trả về BeautifulSoup object.'''
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        response.encoding = 'utf-8' # Cố định encoding nếu cần
        return BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        print(f"Lỗi tải trang {url}: {e}")
        return None

def _safe_filename(s: str) -> str:
    '''Loại bỏ ký tự đặc biệt để làm tên file hợp lệ.'''
    return re.sub(r'[\\/*?:"<>|]', "", s).strip()

def _xhtml_to_txt(xhtml_content: str) -> str:
    '''Chuyển đổi chuỗi XHTML có thẻ <p> sang văn bản TXT thuần tùy.'''
    soup = BeautifulSoup(xhtml_content, 'html.parser')
    return "\n\n".join([p.get_text() for p in soup.find_all('p')])

# ================= PARSER CỦA 52SHUKU =================
def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    info = {}
    h1 = soup.find("h1", class_="article-title")
    if h1:
        info["title"] = _text(h1)
    else:
        div_tag = soup.find("div", class_="content contentmargin")
        if div_tag:
            header = div_tag.find("header", class_="article-header")
            if header:
                h1 = header.find("h1", class_="article-title")
                if h1:
                    info["title"] = _text(h1)
        if "title" not in info:
            h1 = soup.find("h1")
            info["title"] = _text(h1) if h1 else "Không tìm thấy tiêu đề"

    info["introduction"] = "Không tìm thấy phần giới thiệu"
    article = soup.find("article", class_="article-content")
    if article:
        p_tags = article.find_all("p", recursive=False)
        if len(p_tags) >= 2:
            info["introduction"] = _text(p_tags[1])
        else:
            intro_tag = soup.find("p", string=lambda t: t and ("简介" in t or "小说简介：" in t))
            if intro_tag and intro_tag.find_next("p"):
                info["introduction"] = _text(intro_tag.find_next("p"))
    return info

def _get_list_chapters(soup: BeautifulSoup) -> List[Dict[str, str]]:
    chap_list = []
    article = soup.find("article", class_="article-content")
    if not article: return chap_list
    ul_tag = article.find("ul", class_="list clearfix")
    if not ul_tag: return chap_list

    li_tags = ul_tag.find_all("li")
    for li in li_tags:
        a_tag = li.find("a", href=True)
        if a_tag:
            chap_list.append({
                "title": _text(a_tag),
                "url": a_tag["href"]
            })
    return chap_list

def _get_content_chapter(soup: BeautifulSoup) -> str:
    content_div = soup.find("div", id="text")
    if not content_div:
        content_div = soup.find("article", class_="article-content")
    if not content_div:
        return "<p>Không tìm thấy nội dung chương.</p>"

    for unwanted in content_div.find_all(["script", "style", "noscript"]):
        unwanted.decompose()

    paragraphs = []
    for p in content_div.find_all("p"):
        text = _text(p)
        if text: paragraphs.append(f"<p>{text}</p>")
    return "\n".join(paragraphs)

def _get_all_chapter(chap_list: List[Dict[str, str]]) -> List[Dict[str, str]]:
    print(f"\nBắt đầu tải nội dung {len(chap_list)} chương...")
    for i, chap in enumerate(chap_list):
        url = chap["url"]
        print(f"\rĐang tải [{i+1}/{len(chap_list)}]: {chap['title']}", end="")
        soup = _fetch_html(url)
        if soup:
            chap["content"] = _get_content_chapter(soup)
        else:
            chap["content"] = "<p>Lỗi tải chương.</p>"
        time.sleep(SLEEP_BETWEEN_CHAPS)
    print("\nĐã tải xong toàn bộ chương!")
    return chap_list

# ================= MODULE LƯU TRỮ =================
def save_txt_merged(book_info: Dict, chap_list: List[Dict]):
    title = _safe_filename(book_info.get("title", "Truyen_52shuku"))
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    file_path = os.path.join(OUTPUT_DIR, f"{title}.txt")
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(f"{book_info.get('title')}\n")
        f.write(f"{book_info.get('introduction')}\n")
        f.write("="*50 + "\n\n")
        
        for chap in chap_list:
            f.write(f"{chap['title']}\n")
            f.write("-" * 30 + "\n")
            f.write(_xhtml_to_txt(chap["content"]) + "\n\n")
            
    print(f"✔ Đã lưu TXT gộp tại: {file_path}")

def save_txt_split(book_info: Dict, chap_list: List[Dict]):
    title = _safe_filename(book_info.get("title", "Truyen_52shuku"))
    book_dir = os.path.join(OUTPUT_DIR, title)
    os.makedirs(book_dir, exist_ok=True)
    
    for i, chap in enumerate(chap_list):
        safe_chap_title = _safe_filename(chap["title"])
        file_name = f"{i+1:04d} - {safe_chap_title}.txt"
        file_path = os.path.join(book_dir, file_name)
        
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"{chap['title']}\n\n")
            f.write(_xhtml_to_txt(chap["content"]))
            
    print(f"✔ Đã lưu {len(chap_list)} chương (TXT tách) tại thư mục: {book_dir}")

def build_epub2(book_info: Dict, chap_list: List[Dict]):
    title = book_info.get("title", "Truyen 52shuku")
    safe_title = _safe_filename(title)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    epub_path = os.path.join(OUTPUT_DIR, f"{safe_title}.epub")
    
    book_uuid = str(uuid.uuid4())
    
    with zipfile.ZipFile(epub_path, 'w') as epub:
        # 1. mimetype (bắt buộc dạng uncompressed theo chuẩn EPUB)
        epub.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        
        # 2. META-INF/container.xml
        container_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>'''
        epub.writestr('META-INF/container.xml', container_xml, compress_type=zipfile.ZIP_DEFLATED)
        
        # 3. Các file HTML cho từng chương
        manifest_items = []
        spine_items = []
        nav_points = []
        
        for i, chap in enumerate(chap_list):
            chap_id = f"chap_{i+1:04d}"
            file_name = f"{chap_id}.xhtml"
            
            # Bọc xhtml cho đúng chuẩn EPUB2
            xhtml = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <title>{chap["title"]}</title>
</head>
<body>
    <h2>{chap["title"]}</h2>
    {chap["content"]}
</body>
</html>'''
            epub.writestr(f"OEBPS/{file_name}", xhtml, compress_type=zipfile.ZIP_DEFLATED)
            
            manifest_items.append(f'<item id="{chap_id}" href="{file_name}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="{chap_id}"/>')
            nav_points.append(f'''
    <navPoint id="navPoint-{i+1}" playOrder="{i+1}">
      <navLabel><text>{chap["title"]}</text></navLabel>
      <content src="{file_name}"/>
    </navPoint>''')

        # 4. OEBPS/toc.ncx (Dành cho trình đọc Kobo/thiết bị cũ)
        toc_ncx = f'''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="urn:uuid:{book_uuid}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>{"".join(nav_points)}
  </navMap>
</ncx>'''
        epub.writestr('OEBPS/toc.ncx', toc_ncx, compress_type=zipfile.ZIP_DEFLATED)

        # 5. OEBPS/content.opf
        content_opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{title}</dc:title>
    <dc:language>zh</dc:language>
    <dc:identifier id="BookId" opf:scheme="UUID">urn:uuid:{book_uuid}</dc:identifier>
    <dc:description>{book_info.get("introduction", "")}</dc:description>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    {"".join(manifest_items)}
  </manifest>
  <spine toc="ncx">
    {"".join(spine_items)}
  </spine>
</package>'''
        epub.writestr('OEBPS/content.opf', content_opf, compress_type=zipfile.ZIP_DEFLATED)

    print(f"✔ Đã build thành công EPUB 2 tại: {epub_path}")


# ================= MAIN MENU =================
def main():
    print("="*40)
    print("TOOL TẢI TRUYỆN 52SHUKU.NET")
    print("="*40)
    
    url = input("Nhập URL truyện (VD: https://www.52shuku.net/yanqing/...): ").strip()
    if not url:
        print("URL trống, thoát chương trình.")
        return

    print("Đang quét thông tin...")
    soup = _fetch_html(url)
    if not soup: return

    book_info = _get_book_info(soup)
    print(f"\n>> Tiêu đề: {book_info.get('title')}")
    intro = book_info.get('introduction', '')
    print(f">> Giới thiệu: {intro[:100]}..." if len(intro) > 100 else f">> Giới thiệu: {intro}")

    chap_list = _get_list_chapters(soup)
    print(f">> Tổng số chương: {len(chap_list)}\n")
    if not chap_list: return

    # In Menu
    print("CHỌN ĐỊNH DẠNG TẢI:")
    print("[1] : TXT (Gộp 1 file duy nhất)")
    print("[2] : TXT (Tách từng chương)")
    print("[3] : EPUB 2 (Hỗ trợ Kobo)")
    
    choice = input("\nNhập lựa chọn của bạn (1/2/3): ").strip()
    if choice not in ['1', '2', '3']:
        print("Lựa chọn không hợp lệ, thoát chương trình.")
        return

    # Kích hoạt quá trình tải HTML từng chương
    chap_list = _get_all_chapter(chap_list)

    # Chạy module lưu trữ theo Option
    print("\nĐang đóng gói file, vui lòng chờ...")
    if choice == '1':
        save_txt_merged(book_info, chap_list)
    elif choice == '2':
        save_txt_split(book_info, chap_list)
    elif choice == '3':
        build_epub2(book_info, chap_list)

if __name__ == "__main__":
    main()