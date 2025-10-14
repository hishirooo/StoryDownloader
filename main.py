# -*- coding: utf-8 -*-
import importlib, os, sys, re, unicodedata
from urllib.parse import urlparse
from bs4 import BeautifulSoup  # <-- MỚI: dùng để tách TXT từ HTML đã tải

# ===== Helpers =====
def safe_filename_unicode(s: str, limit: int = 150) -> str:
    """Giữ nguyên dấu tiếng Việt; chỉ loại ký tự cấm của filesystem."""
    s = (s or "").strip()
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r'[\\/:*?"<>|\x00-\x1F]+', "—", s)  # Windows-invalid -> em-dash
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(" .") or "untitled"
    return s[:limit].rstrip(" .") or "untitled"

def domain_to_module_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "truyenfull.vision" in host or "truyenfull" in host:
        return "truyenfull_vision"
    if "tangthuvien.net" in host or "truyen.tangthuvien.vn" in host:
        return "tangthuvien_net"
    raise RuntimeError(f"Chưa hỗ trợ domain: {host}")

def load_module(url: str):
    modname = domain_to_module_name(url)
    print(f"→ Ánh xạ domain thành module: {modname}")
    module = importlib.import_module(modname)
    print(f"Đã import module: {modname}")
    return module

# ===== TXT utils =====
def save_chapter_txt(book_dir: str, idx: int, title: str, plain_text: str):
    os.makedirs(book_dir, exist_ok=True)
    fname = f"{idx:04d} - {safe_filename_unicode(title)}.txt"
    path = os.path.join(book_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(title.strip() + "\n\n" + (plain_text or "").strip() + "\n")
    return path

def html_to_text(html_str: str) -> str:
    """Fallback chuyển HTML thô -> TXT (khi không có BeautifulSoup)."""
    s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", html_str or "")
    s = re.sub(r"(?is)<\s*/\s*p\s*>", "\n\n", s)
    s = re.sub(r"(?is)<\s*p[^>]*>", "", s)
    s = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = re.sub(r"\r?\n\s*\r?\n\s*\r?\n+", "\n\n", s)
    return s.strip()

# ---- MỚI: tách tiêu đề + nội dung TXT từ file HTML đã lưu
def extract_title_and_text_from_html_file(path: str):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        html_src = f.read()

    soup = BeautifulSoup(html_src, "html.parser")

    # ưu tiên <h1> đầu trang làm tiêu đề chương (theo template của 2 plugin)
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else os.path.splitext(os.path.basename(path))[0]

    # tìm <article> trước (template của mình), nếu không có thì fallback body
    content_node = soup.find("article") or soup.body or soup
    # thay <br> = newline, <p> = đoạn
    for br in content_node.find_all("br"):
        br.replace_with("\n")
    txt_parts = []
    # nếu có <p>, lấy theo đoạn; nếu không, lấy toàn bộ text
    ps = content_node.find_all("p")
    if ps:
        for p in ps:
            t = p.get_text(" ", strip=True)
            if t:
                txt_parts.append(t)
        text = "\n\n".join(txt_parts).strip()
    else:
        text = content_node.get_text("\n", strip=True)
        # làm gọn khoảng trắng thừa
        text = re.sub(r"\r?\n\s*\r?\n\s*\r?\n+", "\n\n", text)

    return title or "Chương", text or ""

# ---- MỚI: duyệt thư mục, lấy danh sách HTML theo thứ tự 0001, 0002, ...
def find_existing_chapter_htmls(book_dir: str):
    if not os.path.isdir(book_dir):
        return []
    files = [f for f in os.listdir(book_dir) if re.match(r"^\d{4}\s*-\s*.*\.html$", f, flags=re.I)]
    files.sort()  # 0001 ... 000N
    return [os.path.join(book_dir, f) for f in files]

# ===== MAIN =====
if __name__ == "__main__":
    try:
        url = input("Nhập URL: ").strip()
        module = load_module(url)

        # Lấy DS chương + meta
        meta = module.getText(url)
        title    = meta.get("title")  or "Truyện"
        author   = meta.get("author") or "—"
        genres   = meta.get("genres") or []
        status   = meta.get("status") or meta.get("tinh_trang") or ""
        chapters = meta.get("chapters") or []
        total_pages = meta.get("total_pages", 1)

        print("====== THÔNG TIN TRUYỆN ======")
        print(f"Tiêu đề : {title}")
        print(f"Tác giả : {author}")
        if genres: print("Thể loại: " + ", ".join(genres))
        if status: print(f"Tình trạng: {status}")
        print(f"Số chương tìm thấy: {len(chapters)} (tổng trang mục lục: {total_pages})")

        # Hỏi chế độ
        print("\n=== Chọn chế độ tải/xuất ===")
        print("1. Tải truyện - HTML")
        print("2. Tải truyện - TXT")
        print("3. Tải truyện HTML + Tạo EPUB")
        print("4. Tải truyện TXT + Tạo EPUB")
        print("5. Tải truyện TXT + HTML + Tạo EPUB")
        print("6. Tạo KEPUB (EPUB3 + kobo.js)")
        choice = (input("Chọn [1-6]: ").strip() or "1")

        do_html = choice in {"1","3","5","6"}    # 6 cũng xuất HTML (tiện kiểm)
        do_txt  = choice in {"2","4","5"}
        do_epub = choice in {"3","4","5","6"}
        kepub_mode = choice == "6"

        # Thư mục output (giữ nguyên dấu)
        out_root = "output"
        book_dir = os.path.join(out_root, safe_filename_unicode(title))
        os.makedirs(book_dir, exist_ok=True)

        # ===== HTML =====
        saved_html = []
        if do_html:
            print("\n— Lưu HTML từng chương …")
            saved_html = module.save_all_chapters_to_html(
                book_title=title,
                chapters=chapters,
                out_dir=book_dir,
            )
            # tạo index
            if hasattr(module, "save_index_html"):
                module.save_index_html(book_title=title, author=author, genres=genres, status=status,
                                       chapters=chapters, out_dir=book_dir)
            print(f"✔ Đã lưu {len(saved_html)} file HTML vào: {book_dir}")

        # ===== TXT ===== (ƯU TIÊN TRÍCH TỪ HTML ĐÃ CÓ)
        if do_txt:
            print("\n— Lưu TXT từng chương …")
            count = 0
            html_sources = []

            if saved_html:
                # vừa tải HTML xong → dùng luôn theo đúng thứ tự
                html_sources = saved_html
            else:
                # tìm HTML đã có từ lần trước trong thư mục
                html_sources = find_existing_chapter_htmls(book_dir)

            if html_sources:
                # trích TXT từ HTML có sẵn
                for idx, html_path in enumerate(html_sources, 1):
                    ch_title, ch_text = extract_title_and_text_from_html_file(html_path)
                    save_chapter_txt(book_dir, idx, ch_title, ch_text)
                    count += 1
            else:
                # fallback: chưa có HTML → fetch trực tiếp từ site (không lưu HTML)
                for i, info in enumerate(chapters, 1):
                    chap = module.fetch_chapter_content(info["url"])
                    ctitle = chap.get("title") or info.get("title") or f"Chương {i}"
                    ctext  = html_to_text(chap.get("content_html") or "")
                    save_chapter_txt(book_dir, i, ctitle, ctext)
                    count += 1

            print(f"✔ Đã lưu {count} file TXT vào: {book_dir}")

        # ===== EPUB / KEPUB =====
        if do_epub:
            from epub_builder import create_epub

            user_cover = input("\nNhập đường dẫn ảnh cover (bỏ trống để lấy từ trang nếu có): ").strip()
            cover_bytes, cover_ext = None, ".jpg"
            if (not user_cover) and hasattr(module, "_fetch_cover_from_book_page") and hasattr(module, "_clean_to_list_url"):
                try:
                    cb, ce, _ = module._fetch_cover_from_book_page(module._clean_to_list_url(url))
                    if cb:
                        cover_bytes, cover_ext = cb, (ce or ".jpg")
                except Exception:
                    pass

            if kepub_mode:
                kobo_js = input("Nhập đường dẫn kobo.js (bỏ trống = không nhúng): ").strip()
                out_name = f"{safe_filename_unicode(title)}.kepub.epub"
                print("\n— Tạo KEPUB (EPUB3 + kobo.js)…")
                epub_path = create_epub(
                    book_url=url,
                    book_title=title,
                    author=author,
                    chapters=chapters,
                    fetch_fn=module.fetch_chapter_content,
                    out_dir=book_dir,                 # xuất ngay trong thư mục sách
                    cover_path=user_cover or None,
                    cover_bytes=cover_bytes,
                    cover_ext=cover_ext,
                    epub_version=3,
                    language="vi",
                    creator="Hishiro",
                    with_kobo_js=True,
                    kobo_js_path=kobo_js or None,
                    out_filename=out_name,            # tên file giữ dấu
                )
                print(f"✔ KEPUB: {epub_path}")
            else:
                print("\nChuẩn EPUB: [3] EPUB3 (mặc định) | [2] EPUB2 (Kobo-friendly)")
                mode = input("Nhập 3 hoặc 2 (Enter = 3): ").strip()
                epub_version = 3 if mode != "2" else 2

                out_name = f"{safe_filename_unicode(title)}.epub"
                print("\n— Tạo EPUB …")
                epub_path = create_epub(
                    book_url=url,
                    book_title=title,
                    author=author,
                    chapters=chapters,
                    fetch_fn=module.fetch_chapter_content,
                    out_dir=book_dir,
                    cover_path=user_cover or None,
                    cover_bytes=cover_bytes,
                    cover_ext=cover_ext,
                    epub_version=epub_version,
                    language="vi",
                    creator="Hishiro",
                    with_kobo_js=False,
                    out_filename=out_name,            # tên file giữ dấu
                )
                print(f"✔ EPUB: {epub_path}")

        print("\nHoàn tất.")
    except KeyboardInterrupt:
        print("\nĐã hủy.")
    except Exception as e:
        print(f"Lỗi: {e}")
        sys.exit(1)
