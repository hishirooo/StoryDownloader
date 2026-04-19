import os
import re
import time
import uuid
import zipfile
import threading
import requests
from bs4 import BeautifulSoup
from typing import List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback

# =============== CẤU HÌNH ===============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
# Cấu hình an toàn dựa trên kết quả test:
MAX_WORKERS = 5
SLEEP_BETWEEN_CHAPS = 2.0 
OUTPUT_BASE = "Output"
print_lock = threading.Lock()

# ================= HELPER =================
def _text(el) -> str:
    try:
        if not el: return ""
        text = el.get_text(" ", strip=True)
        text = text.replace('\u3000', ' ').replace('\xa0', ' ')
        return re.sub(r'\s+', ' ', text).strip()
    except Exception:
        return ""

def _fetch_html(url: str) -> BeautifulSoup:
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        response.encoding = 'utf-8'
        return BeautifulSoup(response.text, "html.parser")
    except Exception:
        return None

def _safe_filename(s: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", s).strip()

def _wrap_xhtml(title: str, content: str) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title></head>
<body><h2>{title}</h2>{content}</body>
</html>'''

# ================= PARSER 52SHUKU =================
def _get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    info = {"title": "Unknown", "introduction": ""}
    h1 = soup.find("h1", class_="article-title")
    if h1: info["title"] = _text(h1)
    
    article = soup.find("article", class_="article-content")
    if article:
        p_tags = article.find_all("p", recursive=False)
        if len(p_tags) >= 2: info["introduction"] = _text(p_tags[1])
    return info

def _get_list_chapters(soup: BeautifulSoup) -> List[Dict[str, str]]:
    chap_list = []
    article = soup.find("article", class_="article-content")
    if not article: return []
    ul_tag = article.find("ul", class_="list clearfix")
    if not ul_tag: return []
    for li in ul_tag.find_all("li"):
        a = li.find("a", href=True)
        if a: chap_list.append({"title": _text(a), "url": a["href"]})
    return chap_list

def _get_content_chapter(soup: BeautifulSoup) -> str:
    div = soup.find("div", id="text") or soup.find("article", class_="article-content")
    if not div: return ""
    for r in div.find_all(["script", "style"]): r.decompose()
    paras = [_text(p) for p in div.find_all("p") if _text(p)]
    return "\n".join([f"<p>{p}</p>" for p in paras]) if paras else ""

# ================= XỬ LÝ LƯU TRỮ =================
def save_chapter_to_disk(book_dir: str, index: int, chap: Dict, mode: str):
    safe_title = _safe_filename(chap['title'])
    
    if mode in ['1', '2']:
        txt_path = os.path.join(book_dir, f"{index:04d} - {safe_title}.txt")
        clean_txt = chap['content'].replace("<p>", "").replace("</p>", "\n\n")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"{chap['title']}\n\n{clean_txt}")
            
    if mode == '3':
        xhtml_path = os.path.join(book_dir, f"chap_{index:04d}.xhtml")
        with open(xhtml_path, "w", encoding="utf-8") as f:
            f.write(_wrap_xhtml(chap['title'], chap['content']))

# ================= MULTI-THREADING ENGINE =================
def _download_worker(chap: Dict, index: int, book_dir: str, mode: str):
    soup = _fetch_html(chap['url'])
    content = _get_content_chapter(soup) if soup else ""
    
    with print_lock:
        if not content:
            print(f"  [!] Thất bại: {chap['title']}")
            chap['content'] = "" 
            return False
        else:
            print(f"  [OK] Đã tải: {chap['title']}")
            chap['content'] = content
            save_chapter_to_disk(book_dir, index, chap, mode)
            
    # Ngủ đông trong luồng để giãn cách Request
    time.sleep(SLEEP_BETWEEN_CHAPS)
    return True

def download_manager(book_dir: str, selected_chaps: List[Dict], mode: str):
    failed_indices = []
    
    print(f"\n🚀 Đang tải song song với {MAX_WORKERS} luồng...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_download_worker, chap, i + 1, book_dir, mode): i 
            for i, chap in enumerate(selected_chaps)
        }
        for future in as_completed(futures):
            idx = futures[future]
            if not future.result():
                failed_indices.append(idx)

    # Retry Lượt 2 (Đơn luồng & Cầm chừng)
    if failed_indices:
        print(f"\n(!) Đang thử tải lại {len(failed_indices)} chương lỗi...")
        for i in failed_indices:
            chap = selected_chaps[i]
            time.sleep(SLEEP_BETWEEN_CHAPS) # Nghỉ trước khi thử lại
            soup = _fetch_html(chap['url'])
            content = _get_content_chapter(soup) if soup else ""
            if content:
                chap['content'] = content
                save_chapter_to_disk(book_dir, i + 1, chap, mode)
                print(f"  [Fixed] {chap['title']}")
            else:
                chap['content'] = "<p><i>(Lỗi tải nội dung sau nhiều lần thử)</i></p>"
                save_chapter_to_disk(book_dir, i + 1, chap, mode)
                print(f"  [Bỏ qua] {chap['title']} vẫn lỗi.")

def build_epub(book_dir: str, book_info: Dict, chap_list: List[Dict]):
    title = book_info['title']
    epub_path = os.path.join(OUTPUT_BASE, f"{_safe_filename(title)}.epub")
    book_uuid = str(uuid.uuid4())
    
    with zipfile.ZipFile(epub_path, 'w') as epub:
        epub.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        epub.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        
        manifest, spine, nav = [], [], []
        for i, chap in enumerate(chap_list):
            file_name = f"chap_{i+1:04d}.xhtml"
            local_path = os.path.join(book_dir, file_name)
            if os.path.exists(local_path):
                epub.write(local_path, f"OEBPS/{file_name}")
                manifest.append(f'<item id="c{i}" href="{file_name}" media-type="application/xhtml+xml"/>')
                spine.append(f'<itemref idref="c{i}"/>')
                nav.append(f'<navPoint id="np{i}" playOrder="{i+1}"><navLabel><text>{chap["title"]}</text></navLabel><content src="{file_name}"/></navPoint>')

        ncx = f'<?xml version="1.0" encoding="UTF-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="{book_uuid}"/></head><docTitle><text>{title}</text></docTitle><navMap>{"".join(nav)}</navMap></ncx>'
        epub.writestr('OEBPS/toc.ncx', ncx)
        
        opf = f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title><dc:identifier id="id">{book_uuid}</dc:identifier></metadata><manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>{"".join(manifest)}</manifest><spine toc="ncx">{"".join(spine)}</spine></package>'
        epub.writestr('OEBPS/content.opf', opf)
    print(f"\n✔ Đã tạo file EPUB thành công: {epub_path}")

# ================= CHƯƠNG TRÌNH CHÍNH =================
def main():
    print("="*40 + "\n52SHUKU MULTI-THREAD DOWNLOADER\n" + "="*40)
    url = input("Nhập URL truyện: ").strip()
    soup = _fetch_html(url)
    if not soup: return

    book_info = _get_book_info(soup)
    all_chapters = _get_list_chapters(soup)
    print(f"\n>> Truyện: {book_info['title']}")
    print(f">> Tổng số: {len(all_chapters)} chương")

    print("\nLỰA CHỌN:")
    print("[1] TXT Gộp 1 file")
    print("[2] TXT Tách từng chương")
    print("[3] EPUB 2 (Hỗ trợ Kobo)")
    print("[4] Tải theo phạm vi (Start-End)")
    
    # Mặc định chọn 3 nếu bỏ trống
    choice = input("\nChọn (1/2/3/4) [Mặc định: 3]: ").strip()
    if not choice:
        choice = '3'
        print("-> Đã chọn mặc định: [3] EPUB")
    
    start, end = 0, len(all_chapters)
    mode = choice

    if choice == '4':
        r_str = input("Nhập phạm vi (VD: 10-50 hoặc Enter để lấy hết): ").strip()
        if r_str:
            try:
                s, e = map(int, r_str.split('-'))
                start, end = s-1, e
            except: pass
        print("Chọn định dạng cho phạm vi này: [1] TXT Gộp | [2] TXT Tách | [3] EPUB")
        mode = input("Chọn [Mặc định: 3]: ").strip()
        if not mode:
            mode = '3'
            print("-> Đã chọn mặc định: [3] EPUB")

    selected = all_chapters[start:end]
    book_folder = os.path.join(OUTPUT_BASE, _safe_filename(book_info['title']))
    os.makedirs(book_folder, exist_ok=True)

    # Thực hiện tải
    download_manager(book_folder, selected, mode)

    # Xử lý sau khi tải xong
    if mode == '1':
        merged_path = os.path.join(book_folder, f"{_safe_filename(book_info['title'])}_GOP.txt")
        with open(merged_path, "w", encoding="utf-8") as f:
            f.write(f"{book_info['title']}\n\n{book_info['introduction']}\n\n" + "="*30 + "\n")
            for c in selected:
                clean = c['content'].replace("<p>", "").replace("</p>", "\n\n")
                f.write(f"\n\n--- {c['title']} ---\n\n{clean}")
        print(f"\n✔ Đã gộp thành công file TXT: {merged_path}")
        
    elif mode == '3':
        build_epub(book_folder, book_info, selected)

    print("\n--- HOÀN THÀNH ---")

if __name__ == "__main__":
    while True:
        try:
            # Xóa sạch màn hình console trước khi bắt đầu tải truyện mới
            os.system('cls' if os.name == 'nt' else 'clear')
            
            # Chạy chương trình chính
            main()
            
        except Exception as e:
            # Bắt lỗi nếu có crash
            print("\n" + "="*40)
            print("[!] ĐÃ XẢY RA LỖI NGHIÊM TRỌNG:")
            print(e)
            print("-" * 40)
            traceback.print_exc()
            print("="*40)
            
        finally:
            # Hỏi người dùng muốn tải tiếp hay nghỉ
            print("\n" + "="*40)
            tiep_tuc = input("Bạn có muốn tải truyện khác không? (y/n) [Mặc định: y]: ").strip().lower()
            
            # Nếu gõ 'n' thì thoát vòng lặp, tắt chương trình
            if tiep_tuc == 'n':
                print("Tạm biệt!")
                break
            # Nếu gõ phím khác hoặc Enter thì vòng lặp while quay lại từ đầu (chạy lại main)