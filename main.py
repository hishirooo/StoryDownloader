# -*- coding: utf-8 -*-
"""
CLI downloader:
- Nhập URL → hỏi cover → menu (HTML/TXT/EPUB)
- TXT chỉ theo từng chương (không gộp all.txt)
- Nếu đã lưu HTML (1/3/4/6), TXT và EPUB sẽ trích từ HTML có sẵn (đỡ tải lại).
"""
import importlib, re, sys, os, io, glob
from urllib.parse import urlparse
from typing import Optional
from download_logger import chapter_log_line

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
    # 22biqu
    "22biqu.com": "22biqu",
    "www.22biqu.com": "22biqu",
    "m.22biqu.com": "m_22biqu",
    "m.22biqu.net": "m_22biqu",
    # 69shuba
    "69shuba.com": "69shuba",
    "www.69shuba.com": "69shuba",
    # balshuzhal
    "balshuzhal.cc": "balshuzhal",
    "www.balshuzhal.cc": "balshuzhal",
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
    # mtruyen
    "mtruyen.net": "mtruyen",
    "www.mtruyen.net": "mtruyen",
    # erciyan
    "erciyan.com": "erciyan",
    "www.erciyan.com": "erciyan",
    # biqu86
    "biqu86.com": "biqu86",
    "www.biqu86.com": "biqu86",
    # biququ
    "biququ.co": "biququ",
    "www.biququ.co": "biququ",
    # bxwx9
    "bxwx9.org": "bxwx9",
    "www.bxwx9.org": "bxwx9",
    # bqxs
    "bqxs.net": "bqxs",
    "www.bqxs.net": "bqxs",
    # bqglll
    "bqglll.cc": "bqglll",
    "www.bqglll.cc": "bqglll",
    "m.bqglll.cc": "bqglll",
    # medoctruyen
    "medoctruyen.vn": "medoctruyen",
    "www.medoctruyen.vn": "medoctruyen",
    # truyencom
    "truyencom.com": "truyencom",
    "www.truyencom.com": "truyencom",
    # kanunu8
    "kanunu8.com": "kanunu8",
    "www.kanunu8.com": "kanunu8",
    # khotruyenchu
    "khotruyenchu.space": "khotruyenchu",
    "www.khotruyenchu.space": "khotruyenchu",
    # metruyen
    "metruyen.fit": "metruyen-fit",
    "www.metruyen.fit": "metruyen-fit",
    # piaotia
    "piaotia.com": "piaotia",
    "www.piaotia.com": "piaotia",
    # uukanshu
    "uukanshu.cc": "uukanshu",
    "www.uukanshu.cc": "uukanshu",
    # xqiushubang
    "xqiushubang.com": "xqiushubang",
    "www.xqiushubang.com": "xqiushubang",
    # novel543
    "novel543.com": "novel543",
    "www.novel543.com": "novel543",
    # zhaoshuyuan
    "zhaoshuyuan.net": "zhaoshuyuan",
    "www.zhaoshuyuan.net": "zhaoshuyuan",
    # dienha
    "dienha.com": "dienha",
    "www.dienha.com": "dienha",
    # khoaitay
    "khoaitay.cc": "khoaitay",
    "www.khoaitay.cc": "khoaitay",
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
def _find_html_for_index(out_dir: str, idx: int, total: int) -> Optional[str]:
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
            status = 200
            if reuse_html:
                html_path = _find_html_for_index(out_dir, i, total)
                if html_path and os.path.exists(html_path):
                    text = _extract_text_from_html_file(html_path)
                    title = info.get("title") or f"Chương {i}"
                    status = "CACHE"
                else:
                    c = fetch_fn(info["url"])
                    title = c.get("title") or info.get("title") or f"Chương {i}"
                    status = c.get("status_code", 200)
                    if c.get("text"):
                        text = c["text"]
                    else:
                        from bs4 import BeautifulSoup
                        text = BeautifulSoup(c.get("content_html") or "", "html.parser").get_text("\n", strip=True)
            else:
                c = fetch_fn(info["url"])
                title = c.get("title") or info.get("title") or f"Chương {i}"
                status = c.get("status_code", 200)
                if c.get("text"):
                    text = c["text"]
                else:
                    from bs4 import BeautifulSoup
                    text = BeautifulSoup(c.get("content_html") or "", "html.parser").get_text("\n", strip=True)

            fname = f"{i:04d}.txt"
            with open(os.path.join(txt_dir, fname), "w", encoding="utf-8") as f:
                f.write(f"{title}\n\n{text}\n")
            print(chapter_log_line(i, total, status, i, total, title))
        except Exception as e:
            print(chapter_log_line(i, total, "ERR", i, total, f"{info.get('title') or info.get('url')} ({e})"))

def _ask_int(prompt: str, default=None) -> int:
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        try:
            return int(raw)
        except ValueError:
            print("Vui lòng nhập số hợp lệ.")


def _print_download_menu() -> None:
    print("\n-----------------Menu-----------------")
    print("[1] Tải tất cả ( Html + Epub ) ( Mặc định )")
    print("[2] Tải từ X tới Y ( html )")
    print("[3] Tải chương X ( html )")
    print("[4] Thoát")


def _post_task_menu() -> bool:
    print("\n-----------------Menu-----------------")
    print("[1] Nhập Url truyện mới")
    print("[2] Thoát ( Mặc định )")
    choice = input("Chọn [2]: ").strip() or "2"
    return choice == "1"


def _normalize_range(total: int, start: int = 1, end=None):
    if total <= 0:
        return 1, 0
    start = max(1, int(start or 1))
    end = total if end is None else int(end)
    end = min(total, max(start, end))
    return start, end


def _resolve_fetch_fn(module):
    for name in ("fetch_chapter_content", "get_chapter", "fetch_chapter", "extract_chapter_content"):
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def main():
    import epub_builder
    import download_policy
    while True:
        try:
            url = input("Nhập Url : ").strip()
            if not url:
                print("URL trống, vui lòng nhập lại.")
                continue
            if not re.match(r"^https?://", url, flags=re.I):
                url = "https://" + url

            module_name = get_module_name_from_url(url)
            print(f"→ Ánh xạ domain thành module: {module_name}")
            module = importlib.import_module(module_name)
            print(f"Đã import module: {module_name}")

            if not hasattr(module, "getText"):
                print(f"Module '{module_name}' không có hàm getText(url).")
                continue

            print("Đang lấy thông tin truyện...")
            data = module.getText(url)
            if not data:
                print("Plugin không trả về dữ liệu.")
                continue

            slugify_vi = getattr(
                module,
                "slugify_vi",
                lambda s: re.sub(r"[^a-z0-9]+", "-", (s or 'truyen').lower()).strip("-"),
            )

            title = data.get("title") or "Truyện"
            author = data.get("author") or "—"
            chapters = data.get("chapters", [])
            print("\n====== THÔNG TIN TRUYỆN ======")
            print(f"Tiêu đề : {title}")
            print(f"Tác giả : {author}")
            print(f"Số chương tìm thấy: {len(chapters)}")
            if data.get("category") or data.get("genre") or data.get("genres"):
                print(f"Thể loại: {data.get('category') or data.get('genre') or data.get('genres')}")

            cover_path = input("\nNhập đường dẫn ảnh Cover (bỏ trống để tự lấy): ").strip()
            cover_bytes, cover_ext = _read_cover_from_path(cover_path)
            if not cover_bytes:
                cover_source_url = data.get("url") or url
                cover_bytes, cover_ext = _auto_fetch_cover(module, cover_source_url)

            out_dir = os.path.join("output", slugify_vi(title))
            os.makedirs(out_dir, exist_ok=True)

            fetch_fn = _resolve_fetch_fn(module)
            if fetch_fn is None:
                raise AttributeError(f"Module '{module_name}' thiếu hàm fetch_chapter_content/get_chapter")

            while True:
                _print_download_menu()
                choice = input("Chọn [1]: ").strip() or "1"

                if choice == "1":
                    if module_name == "novel543" and hasattr(module, "save_all_chapters_to_html"):
                        module.save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)
                        epub_chapters = chapters
                        epub_chapters_data = None
                        epub_fetch_fn = fetch_fn
                        epub_cache_dir = out_dir
                    else:
                        download_result = download_policy.download_chapters_with_retries(
                            module=module,
                            book_title=title,
                            chapters=chapters,
                            out_dir=out_dir,
                            fetch_fn=fetch_fn,
                            start=1,
                            end=None,
                            book_url=data.get("url") or url,
                        )
                        epub_chapters = download_result["chapters"]
                        epub_chapters_data = download_result["chapters_data"]
                        epub_fetch_fn = None
                        epub_cache_dir = None
                    epub_path = os.path.join(out_dir, f"{slugify_vi(title)}.epub")
                    epub_builder.create_epub(
                        book_url=url,
                        book_title=title,
                        author=author,
                        chapters=epub_chapters,
                        fetch_fn=epub_fetch_fn,
                        html_cache_dir=epub_cache_dir,
                        chapters_data=epub_chapters_data,
                        cover_bytes=cover_bytes,
                        cover_ext=cover_ext,
                        out_epub_path=epub_path,
                        tags=data.get("category") or data.get("genre") or data.get("genres") or data.get("tags"),
                        book_info=data,
                    )
                    print(f"✔ EPUB đã tạo: {epub_path}")
                    break
                if choice == "2":
                    start = _ask_int("Chương bắt đầu: ")
                    end = _ask_int("Chương kết thúc: ", len(chapters))
                    start, end = _normalize_range(len(chapters), start, end)
                    if module_name == "novel543" and hasattr(module, "save_all_chapters_to_html"):
                        module.save_all_chapters_to_html(title, chapters, out_dir, start=start, end=end)
                    else:
                        download_policy.download_chapters_with_retries(
                            module=module,
                            book_title=title,
                            chapters=chapters,
                            out_dir=out_dir,
                            fetch_fn=fetch_fn,
                            start=start,
                            end=end,
                            book_url=data.get("url") or url,
                        )
                    break
                if choice == "3":
                    idx = _ask_int("Chương cần tải: ")
                    idx, _ = _normalize_range(len(chapters), idx, idx)
                    if module_name == "novel543" and hasattr(module, "save_all_chapters_to_html"):
                        module.save_all_chapters_to_html(title, chapters, out_dir, start=idx, end=idx)
                    else:
                        download_policy.download_chapters_with_retries(
                            module=module,
                            book_title=title,
                            chapters=chapters,
                            out_dir=out_dir,
                            fetch_fn=fetch_fn,
                            start=idx,
                            end=idx,
                            book_url=data.get("url") or url,
                        )
                    break
                if choice == "4":
                    return
                print("Lựa chọn không hợp lệ.")

            if not _post_task_menu():
                return

        except KeyboardInterrupt:
            print("\nĐã hủy.")
            return
        except Exception as e:
            import traceback
            print(f"\nLỖI CHƯƠNG TRÌNH: {e}")
            traceback.print_exc()
        
        
if __name__ == "__main__":
    main()
