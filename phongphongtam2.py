# -*- coding: utf-8 -*-
'''
    @Author: Hishio - Cá mụp cắn cáp
    @github: https://github.com/hishirooo
    @date: 20-11-2025
    @version: 1.4 - Fix EPUB XHTML & packing
    @site: https://phongphongtam2.com/
'''

from bs4 import BeautifulSoup, Comment
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin
import requests, re, os, unicodedata, zipfile, io, time, shutil
from ebooklib import epub
import html
# Pillow (cover)
try:
    from PIL import Image
    HAS_PILLOW = True
except Exception:
    HAS_PILLOW = False
    print("⚠ Cảnh báo: Không tìm thấy thư viện Pillow. Không thể tối ưu/chuyển đổi cover sang JPEG.")

# =============== CẤU HÌNH ===============
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.20
SLEEP_BETWEEN_CHAPS = 1
RETRY_STATUS = {429, 500, 502, 503, 504}
EPUB_TARGET = "epub3"

# Kích thước tối đa cho ảnh bìa (Kobo/e-reader thân thiện)
MAX_COVER_SIZE = (1600, 2400) # (width, height)


def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""


def _safe_filename(s: str) -> str:
    s = s.replace(":", " -").replace("/", " ")
    s = re.sub(r'[\\/*?:"<>|]', "", s)
    return s[:100].strip()


def _slugify_vi(s: str) -> str:
    s = (s or "book").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-_")
    return (s or "book")[:80]


def _clean_chapter_title(raw_title: str, chapter_idx: int) -> str:
    cleaned_title = raw_title or ""
    cleaned_title = re.sub(r"Protected\s*-\s*", "", cleaned_title, flags=re.IGNORECASE)
    cleaned_title = re.sub(r"\[[^\]]+\]", "", cleaned_title).strip()
    m = re.search(r'(chương\s*\d+(\s*[^\d:]*)?)', cleaned_title, re.IGNORECASE)
    return m.group(1).strip().capitalize() if m else f"Chương {chapter_idx}"


def _fetch_html(url: str, password: Optional[str] = None, tries: int = 3, backoff: float = 0.6) -> BeautifulSoup:
    session = requests.Session()
    session.headers.update(HEADERS)
    for k in range(tries):
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code in RETRY_STATUS:
            if k == tries - 1:
                r.raise_for_status()
            time.sleep(backoff*(k+1)); continue
        if 400 <= r.status_code < 500:
            r.raise_for_status()
        r.raise_for_status()
        break
    else:
        raise requests.exceptions.RequestException(f"Failed to fetch HTML for {url} after {tries} attempts.")
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    # handle password form (WordPress) if needed
    password_form = soup.find('form', class_='post-password-form')
    if password_form and password:
        post_url = password_form.get('action') or url
        redirect_to_tag = password_form.find('input', {'name': 'redirect_to'})
        redirect_to = redirect_to_tag.get('value') if redirect_to_tag else url
        payload = {'post_password': password, 'Submit': 'Enter', 'redirect_to': redirect_to}
        r_post = session.post(post_url, data=payload, allow_redirects=False, timeout=TIMEOUT)
        if r_post.status_code in (302, 200):
            r_unlocked = session.get(url, timeout=TIMEOUT)
            if not r_unlocked.encoding or r_unlocked.encoding.lower() == "iso-8859-1":
                r_unlocked.encoding = r_unlocked.apparent_encoding
            r_unlocked.raise_for_status()
            return BeautifulSoup(r_unlocked.text, "html.parser")
    return soup


#----------------------INFO TRUYỆN----------------------#
def _get_book_info(soup: BeautifulSoup) -> dict:
    list_info = {}
    title_node = _text(soup.find("div", class_="post-title")).split("–")
    title = title_node[0].strip() if title_node else "Unknown"
    author = title_node[1].strip() if len(title_node) > 1 else "Unknown"
    list_info['title'] = title
    list_info['author'] = author

    items = soup.find_all('div', class_='post-content_item')
    list_info['genre'] = "N/A"
    for item in items:
        heading = item.find('div', class_='summary-heading')
        if heading and 'Thể loại' in heading.text:
            content = item.find('div', class_='summary-content')
            if content:
                list_info['genre'] = content.text.strip()
                break

    image_node = soup.find("div", class_="summary_image")
    if image_node:
        img_tag = image_node.find('img')
        if img_tag and img_tag.has_attr('src'):
            list_info['cover'] = img_tag['src']

    for item in items:
        heading = item.find('div', class_='summary-heading')
        if heading and 'Trạng thái' in heading.text:
            content = item.find('div', class_='summary-content')
            if content:
                list_info['status'] = content.text.strip()
                break
    return list_info


def _get_list_chapters(url: str) -> list:
    chapters = []
    url_post = url.rstrip("/") + "/ajax/chapters"
    querystring = {"t": "1"}
    payload = ""
    headers = {
        "accept": "*/*",
        "accept-language": "vi,en-US;q=0.9,en;q=0.8",
        "origin": "https://phongphongtam2.com",
        "referer": url,
        "user-agent": HEADERS["User-Agent"],
        "x-requested-with": "XMLHttpRequest"
    }
    response = requests.post(url_post, data=payload, headers=headers, params=querystring)
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, "html.parser")
        ul_node = soup.find("ul", class_="main version-chap no-volumn")
        if ul_node:
            a_node = ul_node.find_all("a")
            for a in a_node:
                link = a.get("href")
                title = _text(a)
                if link:
                    chapters.append({"title": title, "link": link})
    return chapters


def _clean_xhtml_body(content: str) -> str:
    """
    Nhận nội dung HTML (string), trả về chỉ phần thân hợp lệ cho EPUB:
    - loại bỏ xml/doctype/html/head/body nếu có
    - giữ lại các <p> và escape các ký tự cần thiết nếu có
    """
    if not content:
        return ""
    # nếu content là BeautifulSoup Tag -> chuyển về string
    if not isinstance(content, str):
        content = str(content)

    # xóa xml declaration, doctype, html, head, body tags nếu có
    content = re.sub(r'^\s*<\?xml[^>]+\?>', '', content, flags=re.IGNORECASE).strip()
    content = re.sub(r'<!doctype[^>]*>', '', content, flags=re.IGNORECASE)
    content = re.sub(r'</?(html|head|body|title)[^>]*>', '', content, flags=re.IGNORECASE)

    # remove comments
    content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)

    # parse again and keep only <p> (and <img> if you want; here we keep <p>, <br>, <strong>, <em>, <a>, <img>)
    soup = BeautifulSoup(content, "html.parser")
    allowed = {'p', 'br', 'strong', 'b', 'em', 'i', 'a', 'img', 'span', 'u'}
    fragments = []
    for tag in soup.find_all(recursive=False):
        if getattr(tag, 'name', None) in allowed:
            fragments.append(str(tag))
        else:
            # unwrap tags that contain <p> inside or contain text
            inner_ps = tag.find_all('p')
            if inner_ps:
                for p in inner_ps:
                    fragments.append(str(p))
            else:
                # fallback: if tag has text, wrap it into <p>
                txt = tag.get_text(" ", strip=True)
                if txt:
                    fragments.append(f"<p>{txt}</p>")

    body = "\n".join(fragments).strip()

    # final safety: ensure body only contains <p> or allowed tags; if body empty, keep original text as <p>
    if not body:
        text = soup.get_text("\n", strip=True)
        if text:
            body = "<p>" + text + "</p>"
        else:
            body = ""

    return body


def _make_full_xhtml(title: str, body_html: str) -> str:
    title_esc = title.replace("&", "&amp;")
    template = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="vi">
<head>
  <meta charset="utf-8" />
  <title>{title_esc}</title>
</head>
<body>
<h2>{title_esc}</h2>
{body_html}
</body>
</html>'''
    return template


def _get_content_chapter(url: str) -> str:
    """
    Trả về 'clean' HTML body (chuỗi chỉ chứa <p>...).
    """
    soup = _fetch_html(url)
    content_node = soup.find("div", class_="entry-content")
    if not content_node:
        print(f"⚠ Không tìm thấy nội dung tại {url}")
        return ""

    # Remove unwanted <p> (credits/empty)
    for p in list(content_node.find_all('p')):
        txt = p.get_text(" ", strip=True)
        if 'Editor:' in txt or 'Beta:' in txt or txt.strip() == '—' or not txt.strip():
            p.decompose()

    # remove warning divs
    for warning_div in content_node.find_all('div', class_='chapter-warning'):
        warning_div.decompose()

    # remove comments
    for comment in content_node.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # unwrap tags except p, img, br, a, strong, em
    for tag in content_node.find_all():
        if tag.name not in ['p', 'img', 'br', 'a', 'strong', 'b', 'em', 'i', 'span', 'u']:
            try:
                tag.unwrap()
            except Exception:
                pass

    # collect p and allowed inline tags
    ps = content_node.find_all(['p', 'img', 'br'])
    if not ps:
        # fallback: take whole text
        content_html = content_node.get_text("\n", strip=True)
        content_html = "<p>" + content_html + "</p>"
    else:
        content_html = "\n".join([str(p) for p in ps])

    # final clean
    content_html = _clean_xhtml_body(content_html)

    # save raw content for debugging (optional)
    try:
        with open("content.html", "w", encoding="utf-8") as f:
            f.write(content_html)
    except Exception:
        pass
        
    return content_html
def _sniff_image_type(data: bytes) -> tuple[str, str]:
    """Đoán định dạng ảnh từ header."""
    if not data or len(data) < 12: return (".bin", "application/octet-stream")
    if data[:3] == b"\xff\xd8\xff": return (".jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return (".png", "image/png")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return (".webp", "image/webp")
    if data[:6] in (b"GIF87a", b"GIF89a"): return (".gif", "image/gif")
    return (".bin", "application/octet-stream")

def _fetch_and_process_cover(url: str) -> tuple[bytes | None, str | None]:
    """Tải cover, tối ưu (resize/convert sang JPEG) nếu có Pillow."""
    if not url: return None, None
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        img_bytes = r.content
    except Exception as e:
        print(f"⚠ Lỗi tải cover từ {url}: {e}")
        return None, None

    if not HAS_PILLOW:
        ext, _ = _sniff_image_type(img_bytes)
        return img_bytes, ext
        
    try:
        im = Image.open(io.BytesIO(img_bytes))
        
        # Resize/Resample để tối ưu cho e-reader (Kobo)
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
             im.thumbnail(MAX_COVER_SIZE, Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS)
             print(f"✔ Cover resized to: {im.size[0]}x{im.size[1]} (max: {MAX_COVER_SIZE[0]}x{MAX_COVER_SIZE[1]})")
        
        # Convert sang RGB và JPEG
        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
            
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue(), ".jpg"
        
    except Exception as e:
        print(f"⚠ Không convert/resize được cover sang JPEG: {e}. Dùng ảnh gốc.")
        ext, _ = _sniff_image_type(img_bytes)
        return img_bytes, ext

#----------------------TẠO EPUB NÂNG CAO (ĐÃ SỬA LỖI get_item)----------------------#

def create_epub_advanced(info: dict, chapters: list):
    """
    Tải chương, tạo log, và đóng gói EPUB chất lượng cao, Kobo-friendly.
    """
    # ... (Giữ nguyên phần khởi tạo book, info, cover_url) ...
    book_title = info.get('title') or "Truyện không tên"
    book_author = info.get('author') or "Unknown"
    cover_url = info.get('cover')
    
    # 1. Khởi tạo EPUB
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}")
    book.set_title(book_title)
    book.set_language('vi')
    book.add_author(book_author)
    book.add_metadata('DC', 'publisher', book_author)
    book.add_metadata('DC', 'contributor', 'Hishiro')
    
    # 2. Xử lý Cover
    cover_item = None
    if cover_url:
        cover_bytes, cover_ext = _fetch_and_process_cover(cover_url)
        if cover_bytes:
            mime = f"image/{cover_ext.strip('.')}"
            cover_item = epub.EpubItem(
                uid="cover-img", 
                file_name=f"Images/cover{cover_ext}", 
                media_type=mime, 
                content=cover_bytes
            )
            book.add_item(cover_item)
            
            # Đã sửa lỗi TypeError: Bỏ media_type
            book.set_cover("cover-img", cover_item.content)
            
            cover_page = epub.EpubHtml(title='Cover', file_name='Text/cover.xhtml', lang='vi')
            cover_page.content = f'<div style="text-align: center;"><img src="../Images/cover{cover_ext}" alt="Cover"/></div>'
            book.add_item(cover_page)


    # 3. Tải và thêm các chương
    epub_chapters = []
    toc_links = []
    
    total_chapters = len(chapters)
    
    # 3.1. Thêm Trang Bìa vào TOC (Mục lục) và Spine
    if cover_item:
        toc_links.append(epub.Link('Text/cover.xhtml', 'Bìa Truyện', 'cover_page_id')) 
        
    
    for i, chap_info in enumerate(chapters, 1):
        chap_title = chap_info.get('title')
        chap_link = chap_info.get('link')
        clean_title = _clean_chapter_title(chap_title, i)
        
        # 3.2. Lấy nội dung đã làm sạch
        content_body_html = _get_content_chapter(chap_link)
        
        if not content_body_html:
            print(f"[{i:04d}/{total_chapters:04d}] Skipped - Nội dung rỗng.")
            continue
            
        # 3.3. Tạo EpubHtml item
        chap_xhtml = epub.EpubHtml(
            title=clean_title, 
            file_name=f'Text/chap_{i:04d}.xhtml', 
            lang='vi'
        )
        
        chap_xhtml.content = f'<h2>{html.escape(clean_title)}</h2>{content_body_html}'
        
        book.add_item(chap_xhtml)
        epub_chapters.append(chap_xhtml)
        
        # 3.4. Thêm vào TOC và Spine
        toc_links.append(epub.Link(chap_xhtml.file_name, clean_title, f'chap_{i}'))
        
        # LOGGING
        print(f"[{i:04d}/{total_chapters:04d}] Saved   - {clean_title} - {chap_link}")
        
        time.sleep(SLEEP_BETWEEN_CHAPS)

    
    # 4. Thiết lập Mục lục (TOC) và Spine
    book.toc = toc_links
    
    # Sắp xếp Spine: Trang Bìa (nếu có) -> Các Chương
    spine_items = []
    
    # SỬA LỖI: Thay thế get_item() bằng get_items() và lọc.
    if cover_item:
        # Lọc item cover_page ra khỏi tất cả các item
        cover_page_item = next((item for item in book.get_items() if item.file_name == 'Text/cover.xhtml'), None)
        if cover_page_item:
            spine_items.append(cover_page_item) 
    
    spine_items.extend(epub_chapters)
    book.spine = spine_items
    
    # Thêm NCX và NAV (bắt buộc cho EPUB3/TOC)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    # 5. Đóng gói
    out_dir = "output"
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"{_slugify_vi(book_title)}.epub")
    
    try:
        epub.write_epub(out_file, book, {})
        print(f"\n✅ EPUB '{book_title}' đã tạo thành công tại: {out_file}")
    except Exception as e:
        print(f"\n❌ Lỗi khi đóng gói EPUB: {e}")
        
    return out_file
if __name__ == "__main__":
    UrlStory= "https://phongphongtam2.com/manga/tinh-yeu-den-muon-diep-kien-tinh/"
    info = _get_book_info(_fetch_html(UrlStory))
    print("-----------------------------INFO TRUYỆN-----------------------------")
    print(f"Title : {info.get('title')}")
    print(f"Author: {info.get('author')}")
    print(f"Genre : {info.get('genre')}")
    print(f"Status : {info.get('status')}")
    print(f"Cover URL : {info.get('cover')}")

    chapters = _get_list_chapters(UrlStory)
    
    if not chapters:
        print("❌ Không tìm thấy chương nào. Vui lòng kiểm tra lại URL truyện.")
    else:
        print(f"Total chapters: {len(chapters)}")
        
        # Gọi hàm create_epub_advanced với TOÀN BỘ list chapters
        print("\n-----------------------------BẮT ĐẦU TẠO EPUB TOÀN BỘ CHƯƠNG-----------------------------")
        create_epub_advanced(info, chapters)
