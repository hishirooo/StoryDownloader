# -*- coding: utf-8 -*-
import os, sys, io, re, argparse
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# UTF-8 console (Windows)
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
        sys.stdin  = io.TextIOWrapper(sys.stdin.buffer,  encoding="utf-8")
    os.system("chcp 65001 >NUL")

import tangthuvien_net as ttv
from epub_builder import create_epub  # giữ API cũ

def parse_args():
    ap = argparse.ArgumentParser(description="Story Downloader for truyen.tangthuvien.vn")
    ap.add_argument("--delay", type=float, default=0.35, help="Độ trễ giữa các request (giây). Mặc định 0.35")
    ap.add_argument("--limit", type=int, default=75, help="Giới hạn chương mỗi trang của API (limit). Mặc định 75")
    return ap.parse_args()

def _sanitize(s: str) -> str:
    s = s.strip().replace(":", " -")
    return re.sub(r"[^-\w\s\.,\(\)\[\]]+", "", s)

def _read_cover_file(path: str):
    if not path:
        return None, None
    p = Path(path)
    if not p.exists() or not p.is_file():
        print("⚠ Không tìm thấy ảnh bìa, sẽ thử lấy từ trang.")
        return None, None
    return p.read_bytes(), (p.suffix.lstrip(".") or "jpg")

def _download_bytes(url: str):
    import requests
    r = requests.get(url, timeout=20)
    if r.status_code == 200 and r.content:
        ext = (Path(url).suffix or ".jpg").lstrip(".")
        return r.content, ext
    return None, None

def _dump_html(chapters, fetch_fn, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, ch in enumerate(chapters, 1):
        html_content = fetch_fn(ch["url"])
        name = f"{i:04d} - {_sanitize(ch.get('title') or f'Chuong {i}')}.html"
        (out_dir / name).write_text(html_content, encoding="utf-8")
        if i % 10 == 0: print(f"… HTML {i}/{len(chapters)}")

def _dump_txt(chapters, fetch_fn, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, ch in enumerate(chapters, 1):
        plain = BeautifulSoup(fetch_fn(ch["url"]), "html.parser").get_text("\n", strip=True)
        name = f"{i:04d} - {_sanitize(ch.get('title') or f'Chuong {i}')}.txt"
        (out_dir / name).write_text(plain, encoding="utf-8")
        if i % 10 == 0: print(f"… TXT {i}/{len(chapters)}")

def main():
    args = parse_args()

    print("Nhập URL:")
    url = input("> ").strip()

    print("Nhập đường dẫn ảnh bìa (bỏ trống nếu muốn tự lấy từ trang):")
    cover_path = input("> ").strip()
    cover_bytes, cover_ext = _read_cover_file(cover_path)

    if "tangthuvien" not in (urlparse(url).netloc or "").lower():
        print("⚠ Bản này xử lý site truyen.tangthuvien.vn")
        return

    print("Đang lấy meta + chapter…")
    title, author, genres, status, cover_url, chapters, fetch_fn = ttv.fetch_book_meta_and_chapters(
        url, delay=args.delay, per_page=args.limit
    )

    print("\n===== THÔNG TIN TRUYỆN =====")
    print(f"Tiêu đề : {title}")
    if author: print(f"Tác giả : {author}")
    if genres: print(f"Thể loại: {', '.join(genres)}")
    if status: print(f"Tình trạng: {status}")
    print(f"Đã thu link chương: {len(chapters)}")
    if cover_bytes is None and cover_url:
        print("→ Đang tải ảnh bìa từ trang…")
        cover_bytes, cover_ext = _download_bytes(cover_url)

    print("\nChọn chế độ tải:")
    print("1. Tải truyện → HTML")
    print("2. Tải truyện → TXT (tách chương)")
    print("3. Tải truyện HTML + Tạo EPUB")
    print("4. Tải truyện TXT (tách chương) + Tạo EPUB")
    print("5. Tải truyện TXT (tách chương) + HTML + Tạo EPUB")
    try:
        mode = int(input("Nhập số (1-5): ").strip() or "3")
    except Exception:
        mode = 3

    out_dir = Path(_sanitize(title or "truyen"))
    out_dir.mkdir(parents=True, exist_ok=True)

    if mode in (1,3,5):
        _dump_html(chapters, fetch_fn, out_dir / "html")

    if mode in (2,4,5):
        _dump_txt(chapters, fetch_fn, out_dir / "txt")

    if mode in (3,4,5):
        try:
            create_epub(
                url=url, title=title, author=author, chapters=chapters,
                fetch_fn=fetch_fn, cover_bytes=cover_bytes, cover_ext=cover_ext,
                language="vi"
            )
            print(f"✔ EPUB: {title}.epub")
        except Exception as e:
            print(f"⚠ EPUB lỗi: {e}")

    print("\nHoàn tất.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nĐã hủy.")
