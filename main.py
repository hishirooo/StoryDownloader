# -*- coding: utf-8 -*-
"""
CLI downloader:
- Nhập URL → hỏi cover → menu (HTML/TXT/EPUB)
- TXT chỉ theo từng chương (không gộp all.txt)
- Nếu đã lưu HTML (1/3/4/6), TXT và EPUB sẽ trích từ HTML có sẵn (đỡ tải lại).
"""
import importlib, re, sys, os, io, glob
from urllib.parse import urlparse

# ---- Fix UTF-8 cho Windows console ----
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
        sys.stdin  = io.TextIOWrapper(sys.stdin.buffer,  encoding="utf-8")
    os.system("chcp 65001 >NUL")

DOMAIN_MODULE_MAP = {
    # truyenfull
    "truyenfull.vision": "truyenfull_vision",
    "www.truyenfull.vision": "truyenfull_vision",
    "m.truyenfull.vision": "truyenfull_vision",
    # tangthuvien
    "tangthuvien.net": "tangthuvien_net",
    "www.tangthuvien.net": "tangthuvien_net",
    "m.tangthuvien.net": "tangthuvien_net",
    "tangthuvien.vn": "tangthuvien_net",
    "www.tangthuvien.vn": "tangthuvien_net",
    "truyen.tangthuvien.vn": "tangthuvien_net",
    "m.truyen.tangthuvien.vn": "tangthuvien_net",
}

def _strip_common_prefixes(domain: str) -> str:
    return domain.split(":")[0].lower().strip()

def get_module_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    domain = _strip_common_prefixes(parsed.netloc)
    if domain in DOMAIN_MODULE_MAP:
        return DOMAIN_MODULE_MAP[domain]
    return re.sub(r"[^a-z0-9_.-]", "", domain).replace(".", "_")

# ---------------- Cover helpers ----------------
def _read_cover_from_path(path: str):
    if not path or not os.path.exists(path):
        return None, None
    with open(path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(path)[1].lower() or ".jpg"
    return data, (".png" if ext == ".png" else ".jpg")

def _auto_fetch_cover(module, book_page_url: str):
    """Tự lấy cover bằng API của plugin site (nếu có)."""
    for name in ("_fetch_cover_from_book_page", "fetch_cover_from_book_page"):
        fn = getattr(module, name, None)
        if callable(fn):
            try:
                content, ext, src = fn(book_page_url)
                if content:
                    print(f"✓ Tự lấy cover: {src}")
                    return content, (ext or ".jpg")
            except Exception as e:
                print(f"⚠ Không lấy được cover tự động: {e}")
    return None, None

# ---------------- HTML/TXT reuse helpers ----------------
def _find_html_for_index(out_dir: str, idx: int, total: int) -> str | None:
    """
    Tìm file HTML đã lưu ứng với chỉ số chương:
    - Hỗ trợ 0001.html (tangthuvien_net) và "0001 - title.html" (truyenfull_vision).
    """
    pad = len(str(total))
    patterns = [
        os.path.join(out_dir, f"{idx:0{pad}}.html"),
        os.path.join(out_dir, f"{idx:04d}.html"),
        os.path.join(out_dir, f"{idx:0{pad}} *.html"),
        os.path.join(out_dir, f"{idx:04d} *.html"),
        os.path.join(out_dir, f"{idx:0{pad}}-*.html"),
        os.path.join(out_dir, f"{idx:04d}-*.html"),
    ]
    for pat in patterns:
        matches = glob.glob(pat)
        if matches:
            return matches[0]
    matches = [p for p in glob.glob(os.path.join(out_dir, "*.html"))
               if os.path.basename(p).startswith(f"{idx:0{pad}}") or os.path.basename(p).startswith(f"{idx:04d}")]
    return matches[0] if matches else None

def _extract_text_from_html_file(html_path: str) -> str:
    from bs4 import BeautifulSoup
    with open(html_path, "r", encoding="utf-8") as f:
        html_str = f.read()
    soup = BeautifulSoup(html_str, "html.parser")
    node = (soup.select_one(".chapter") or soup.select_one("article") or soup.body or soup)
    text = node.get_text("\n", strip=True)
    return re.sub(r"\s+\n", "\n", text).strip()

def _extract_content_html_from_saved(html_path: str) -> str:
    """Lấy phần HTML nội dung (không convert sang text)."""
    from bs4 import BeautifulSoup
    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")
    node = (soup.select_one(".chapter") or soup.select_one("article") or soup.body or soup)
    return "".join(str(x) for x in node.contents) if node else ""

def _save_txt_per_chapter(module, book_title: str, chapters: list, out_dir: str,
                          fetch_fn, reuse_html: bool):
    """Lưu TXT từng chương; nếu có HTML thì trích từ HTML, tránh tải lại."""
    os.makedirs(out_dir, exist_ok=True)
    txt_dir = os.path.join(out_dir, "txt")
    os.makedirs(txt_dir, exist_ok=True)

    total = len(chapters)
    for i, info in enumerate(chapters, 1):
        try:
            if reuse_html:
                html_path = _find_html_for_index(out_dir, i, total)
                if html_path and os.path.exists(html_path):
                    text = _extract_text_from_html_file(html_path)
                    title = info.get("title") or f"Chương {i}"
                else:
                    c = fetch_fn(info["url"])
                    title = c.get("title") or info.get("title") or f"Chương {i}"
                    if c.get("text"):
                        text = c["text"]
                    else:
                        from bs4 import BeautifulSoup
                        text = BeautifulSoup(c.get("content_html") or "", "html.parser").get_text("\n", strip=True)
            else:
                c = fetch_fn(info["url"])
                title = c.get("title") or info.get("title") or f"Chương {i}"
                if c.get("text"):
                    text = c["text"]
                else:
                    from bs4 import BeautifulSoup
                    text = BeautifulSoup(c.get("content_html") or "", "html.parser").get_text("\n", strip=True)

            fname = f"{i:04d}.txt"
            with open(os.path.join(txt_dir, fname), "w", encoding="utf-8") as f:
                f.write(f"{title}\n\n{text}\n")
            print(f"[{i:04d}/{total}] Saved TXT: {fname}")
        except Exception as e:
            print(f"[{i:04d}/{total}] ERROR TXT: {info.get('url')} — {e}")

def main():
    import epub_builder  # create_epub(book_url, book_title, author, chapters, fetch_fn, ...)
    try:
        # 1) Nhập URL
        url = input("Nhập URL: ").strip()
        if not re.match(r"^https?://", url, flags=re.I):
            url = "https://" + url

        # 2) Map domain -> module
        module_name = get_module_name_from_url(url)
        print(f"→ Ánh xạ domain thành module: {module_name}")
        module = importlib.import_module(module_name)
        print(f"Đã import module: {module_name}")

        if not hasattr(module, "getText"):
            print(f"Module '{module_name}' không có hàm getText(url)."); return

        # 3) Lấy meta + danh sách chương
        data = module.getText(url)
        if not data:
            print("Plugin không trả về dữ liệu."); return

        slugify_vi = getattr(module, "slugify_vi",
                             lambda s: re.sub(r"[^a-z0-9]+", "-", (s or 'truyen').lower()).strip("-"))

        title    = data.get("title") or "Truyện"
        author   = data.get("author") or "—"
        chapters = data.get("chapters", [])
        print("====== THÔNG TIN TRUYỆN ======")
        print(f"Tiêu đề : {title}")
        print(f"Tác giả : {author}")
        if data.get("total_chapters"):
            print(f"Tổng số chương: {data['total_chapters']}")
        if chapters:
            print("Ví dụ 3 chương đầu:")
            for i, c in enumerate(chapters[:3], 1):
                print(f"  {i:02d}. {c.get('title')} -> {c.get('url')}")

        # 4) Hỏi đường dẫn cover
        cover_path = input("Nhập đường dẫn ảnh Cover (bỏ trống để tự lấy): ").strip()
        cover_bytes, cover_ext = _read_cover_from_path(cover_path)
        if not cover_bytes:
            cover_bytes, cover_ext = _auto_fetch_cover(module, url)

        # 5) Menu lựa chọn
        print("\nChọn chức năng:")
        print("[1]: Tải và lưu dạng HTML")
        print("[2]: Tải và lưu dạng TXT")
        print("[3]: Tải và lưu dạng HTML + TXT")
        print("[4]: Tải và lưu dạng HTML + Build Epub")
        print("[5]: Tải và lưu dạng TXT + Build Epub")
        print("[6]: Tải và lưu dạng HTML + TXT + Build Epub")
        choice = input("Nhập lựa chọn (1-6): ").strip()

        out_dir = os.path.join("output", slugify_vi(title))
        os.makedirs(out_dir, exist_ok=True)

        # fetch_fn thống nhất
        fetch_fn = getattr(module, "fetch_chapter_content", None) or getattr(module, "get_chapter", None)
        if fetch_fn is None:
            raise AttributeError(f"Module '{module_name}' thiếu fetch_chapter_content/get_chapter")

        do_html = choice in ("1", "3", "4", "6")
        do_txt  = choice in ("2", "3", "5", "6")
        do_epub = choice in ("4", "5", "6")

        # 6) HTML
        if do_html and hasattr(module, "save_all_chapters_to_html"):
            print(f"\nBắt đầu lưu HTML vào: {out_dir}")
            module.save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)
            print(f"✔ Đã lưu HTML. Thư mục: {out_dir}")

        # 7) TXT (tận dụng HTML nếu có)
        if do_txt:
            print(f"\nBắt đầu lưu TXT vào: {out_dir}\\txt")
            _save_txt_per_chapter(module, title, chapters, out_dir, fetch_fn, reuse_html=do_html)

        # 8) EPUB — Dùng lại HTML nếu có
        if do_epub and chapters:
            # Map nhanh để tìm file theo URL
            url2meta = { info["url"]: (i, info.get("title")) for i, info in enumerate(chapters, 1) }

            def fetch_from_cache_or_net(page_url: str) -> dict:
                """Wrapper: ưu tiên đọc từ HTML, chỉ gọi mạng nếu thiếu."""
                if do_html and page_url in url2meta:
                    idx, ttl = url2meta[page_url]
                    html_path = _find_html_for_index(out_dir, idx, len(chapters))
                    if html_path and os.path.exists(html_path):
                        content_html = _extract_content_html_from_saved(html_path)
                        title_local  = ttl or f"Chương {idx}"
                        return {"title": title_local, "url": page_url, "content_html": content_html}
                # fallback: gọi plugin
                return fetch_fn(page_url)

            import epub_builder
            epub_name = f"{slugify_vi(title)}_epub_builder.epub"
            epub_path = os.path.join(out_dir, epub_name)
            print(f"\nĐang tạo EPUB bằng epub_builder: {epub_path}")
            epub_builder.create_epub(
                url, title, author, chapters,
                fetch_from_cache_or_net,   # <-- dùng lại HTML
                cover_bytes=cover_bytes, cover_ext=(cover_ext or ".jpg"),
                language="vi", creator="Hishiro"
            )
            # Di chuyển file {title}.epub (nếu epub_builder ghi ở CWD) vào out_dir
            if os.path.exists(title + ".epub"):
                try:
                    os.replace(title + ".epub", epub_path)
                except Exception:
                    import shutil; shutil.move(title + ".epub", epub_path)
            print(f"✔ EPUB đã tạo: {epub_path}")

    except KeyboardInterrupt:
        print("\nĐã hủy.")
    except Exception as e:
        print(f"Lỗi: {e}")

if __name__ == "__main__":
    main()
