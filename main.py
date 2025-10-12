# -*- coding: utf-8 -*-
import importlib, re, sys, os, io
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# Windows console UTF-8
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
    "truyenfull.vision": "truyenfull_vision",
    "www.truyenfull.vision": "truyenfull_vision",
    "m.truyenfull.vision": "truyenfull_vision",
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
    return DOMAIN_MODULE_MAP.get(domain, re.sub(r"[^a-z0-9_.-]", "", domain).replace(".", "_"))

def _slugify_default(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or 'truyen').lower()).strip("-")

def _write_index_fallback(out_dir: str, title: str, author: str, genres: list, status: str, chapters: list):
    import html
    os.makedirs(out_dir, exist_ok=True)
    items=[]
    for i,c in enumerate(chapters,1):
        items.append(f'<li><a href="{i:04d} - {html.escape(c.get("title") or f"Chuong {i}")}.html">{html.escape(c.get("title") or f"Chương {i}")}</a></li>')
    genres_txt=", ".join(genres or [])
    html_doc=f"""<!doctype html>
<html lang="vi"><meta charset="utf-8"><title>{html.escape(title)} — Mục lục</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;line-height:1.7;
      padding:24px;max-width:860px;margin:0 auto;background:#f7f7f9;color:#222}}
h1{{font-size:1.8rem;margin:0 0 .6rem}}
.meta{{color:#555;margin:0 0 1rem}}
ol{{padding-left:1.25rem}}
.badge{{display:inline-block;background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;padding:.1rem .5rem;margin-right:.35rem}}
</style>
<h1>{html.escape(title)}</h1>
<div class="meta">
  <span class="badge">Tác giả: {html.escape(author or "—")}</span>
  <span class="badge">Thể loại: {html.escape(genres_txt or "—")}</span>
  <span class="badge">Tình trạng: {html.escape(status or "—")}</span>
</div>
<ol>{''.join(items)}</ol></html>"""
    with open(os.path.join(out_dir,"index.html"),"w",encoding="utf-8") as f:
        f.write(html_doc)

def _html_to_text(html_str: str) -> str:
    soup = BeautifulSoup(html_str or "", "html.parser")
    for tag in soup(["script","style","noscript","iframe"]): tag.decompose()
    text = soup.get_text("\n", strip=True)
    # nén dòng trống
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)

def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:150] or "chapter"

def save_all_chapters_to_txt_split(book_title: str, chapters: list, fetch_fn, out_dir: str):
    """
    Lưu TXT tách chương: mỗi chương 1 file:
    output/<slug>/<0001 - Tên chương>.txt
    """
    os.makedirs(out_dir, exist_ok=True)
    total = len(chapters)
    for i, info in enumerate(chapters, 1):
        c = fetch_fn(info["url"])
        title = c.get("title") or info.get("title") or f"Chương {i}"
        body  = _html_to_text(c.get("content_html"))
        fn = f"{i:04d} - {_safe_filename(title)}.txt"
        with open(os.path.join(out_dir, fn), "w", encoding="utf-8") as f:
            f.write(title + "\n")
            f.write("-" * len(title) + "\n")
            f.write(body + "\n")
        print(f"[{i:04d}/{total}] Saved TXT: {fn}")

def main():
    try:
        url = input("Nhập URL: ").strip()
        if not re.match(r"^https?://", url, flags=re.I):
            url = "https://" + url

        module_name = get_module_name_from_url(url)
        print(f"→ Ánh xạ domain thành module: {module_name}")
        module = importlib.import_module(module_name)
        print(f"Đã import module: {module_name}")

        if not hasattr(module, "getText"):
            print(f"Module '{module_name}' không có hàm getText(url)."); return

        data = module.getText(url)
        if not data:
            print("Plugin không trả về dữ liệu."); return

        slugify_vi = getattr(module, "slugify_vi", _slugify_default)

        title    = data.get("title") or "Truyện"
        author   = data.get("author") or "—"
        genres   = data.get("genres", []) or []
        status   = data.get("status") or "—"
        chapters = data.get("chapters", [])

        print("====== THÔNG TIN TRUYỆN ======")
        print(f"Tiêu đề : {title}")
        print(f"Tác giả : {author}")
        print(f"Thể loại: {', '.join(genres) or '—'}")
        print(f"Tình trạng: {status}")
        if data.get("total_pages"): print(f"Tổng số trang chương: {data['total_pages']}")
        print(f"Đã thu link chương: {len(chapters)}")

        # Menu
        print("\nChọn chế độ tải:")
        print("1. Tải truyện - HTML")
        print("2. Tải truyện - TXT (tách chương)")
        print("3. Tải truyện HTML + Tạo EPUB")
        print("4. Tải truyện TXT (tách chương) + Tạo EPUB")
        print("5. Tải truyện TXT (tách chương) + HTML + Tạo EPUB")
        choice = input("Nhập số (1-5): ").strip()

        do_html = choice in {"1","3","5"}
        do_txt  = choice in {"2","4","5"}
        do_epub = choice in {"3","4","5"}

        out_dir = os.path.join("output", slugify_vi(title))
        fetch_fn = None
        if hasattr(module, "fetch_chapter_content"):
            fetch_fn = lambda u: module.fetch_chapter_content(u)
        elif hasattr(module, "get_chapter"):
            fetch_fn = lambda u: module.get_chapter(u)

        # HTML
        if do_html:
            if hasattr(module, "save_all_chapters_to_html"):
                print(f"\nBắt đầu lưu HTML vào: {out_dir}")
                module.save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)
            else:
                if not fetch_fn:
                    print("Không có fetch_fn để lưu HTML.")
                else:
                    os.makedirs(out_dir, exist_ok=True)
                    total=len(chapters)
                    for i, info in enumerate(chapters, 1):
                        c = fetch_fn(info["url"])
                        fn = f"{i:04d} - {_safe_filename(c.get('title') or f'Chuong {i}')}.html"
                        with open(os.path.join(out_dir, fn), "w", encoding="utf-8") as f:
                            f.write(c.get("content_html") or "")
                        print(f"[{i:04d}/{total}] Saved HTML")
            # index
            if hasattr(module, "save_index_html"):
                module.save_index_html(out_dir, title, author, genres, status, chapters)
            else:
                _write_index_fallback(out_dir, title, author, genres, status, chapters)
            print("✔ Đã viết index.html")

        # TXT (tách chương)
        if do_txt:
            if not fetch_fn:
                print("Không có fetch_fn để lưu TXT.")
            else:
                save_all_chapters_to_txt_split(title, chapters, fetch_fn, out_dir)
                print("✔ TXT: đã lưu tách chương trong thư mục output.")

        # EPUB
        if do_epub:
            if not fetch_fn:
                print("Bỏ qua EPUB: module không cung cấp fetch_fn.")
            else:
                try:
                    from epub_builder import create_epub
                    cover_bytes=None; cover_ext=".jpg"
                    if hasattr(module, "_fetch_cover_from_book_page") and hasattr(module, "_clean_to_list_url"):
                        try:
                            cb, ce, _ = module._fetch_cover_from_book_page(module._clean_to_list_url(url))
                            if cb: cover_bytes, cover_ext = cb, (ce or ".jpg")
                        except Exception:
                            pass
                    print("\nĐang tạo EPUB…")
                    create_epub(url, title, author, chapters, fetch_fn,
                                cover_bytes=cover_bytes, cover_ext=cover_ext, language="vi")
                    print(f"✔ EPUB: {title}.epub")
                except Exception as e:
                    print(f"⚠ EPUB lỗi: {e}")

        print("\nHoàn tất.")
    except KeyboardInterrupt:
        print("\nĐã hủy.")
    except Exception as e:
        print(f"Lỗi: {e}")

if __name__ == "__main__":
    main()
