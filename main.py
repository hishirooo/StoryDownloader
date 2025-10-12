# -*- coding: utf-8 -*-
"""
Bộ điều phối: nhập URL → ánh xạ sang module theo domain → lấy dữ liệu → lưu HTML + EPUB
- Bổ sung nhận diện domain tangthuvien:
    + https://tangthuvien.net/
    + https://truyen.tangthuvien.vn/
- Giữ nguyên truyenfull (truyenfull.vision)
"""

import importlib, re, sys, os, io
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

# Bảng ánh xạ domain -> tên module (file .py tương ứng)
DOMAIN_MODULE_MAP = {
    # truyenfull
    "truyenfull.vision": "truyenfull_vision",
    "www.truyenfull.vision": "truyenfull_vision",
    "m.truyenfull.vision": "truyenfull_vision",

    # tangthuvien (dùng chung module tangthuvien_net.py)
    "tangthuvien.net": "tangthuvien_net",
    "www.tangthuvien.net": "tangthuvien_net",
    "m.tangthuvien.net": "tangthuvien_net",
    "tangthuvien.vn": "tangthuvien_net",            # phòng trường hợp redirect/thói quen nhập
    "www.tangthuvien.vn": "tangthuvien_net",
    "truyen.tangthuvien.vn": "tangthuvien_net",      # domain bạn nêu
    "m.truyen.tangthuvien.vn": "tangthuvien_net",
}

def _strip_common_prefixes(domain: str) -> str:
    # Không strip ở đây để DOMAIN_MODULE_MAP có thể match chính xác các biến thể
    # (vì ta đã liệt kê cả www./m./truyen. trong map). Tuy nhiên vẫn chuẩn hóa lower + bỏ port.
    return domain.split(":")[0].lower().strip()

def get_module_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    domain = _strip_common_prefixes(parsed.netloc)

    # Ưu tiên map cứng nếu có
    if domain in DOMAIN_MODULE_MAP:
        return DOMAIN_MODULE_MAP[domain]

    # Nếu không nằm trong map, fallback theo quy tắc cũ: thay '.' thành '_'
    # Ví dụ: example.com -> example_com
    module_name = re.sub(r"[^a-z0-9_.-]", "", domain).replace(".", "_")
    return module_name

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

        slugify_vi = getattr(module, "slugify_vi",
                             lambda s: re.sub(r"[^a-z0-9]+", "-", (s or 'truyen').lower()).strip("-"))

        title    = data.get("title") or "Truyện"
        author   = data.get("author") or "—"
        genres   = ", ".join(data.get("genres", [])) or "—"
        chapters = data.get("chapters", [])
        total    = data.get("total_pages")

        print("====== THÔNG TIN TRUYỆN ======")
        print(f"Tiêu đề : {title}")
        print(f"Tác giả : {author}")
        print(f"Thể loại: {genres}")
        if data.get("total_chapters"):
            print(f"Tổng số chương: {data['total_chapters']}")
        if chapters:
            print("Ví dụ 3 chương đầu:")
            for i, c in enumerate(chapters[:3], 1):
                print(f"  {i:02d}. {c.get('title')} -> {c.get('url')}")

        # Lưu HTML (nếu plugin có)
        out_dir = os.path.join("output", slugify_vi(title))
        if hasattr(module, "save_all_chapters_to_html"):
            print(f"\nBắt đầu lưu HTML vào: {out_dir}")
            module.save_all_chapters_to_html(title, chapters, out_dir, start=1, end=None)
            print(f"✔ Đã lưu HTML. Thư mục: {out_dir}")

        # Tạo EPUB (nếu plugin có)
        if hasattr(module, "create_epub"):
            epub_name = f"{slugify_vi(title)}.epub"
            epub_path = os.path.join("output", epub_name)
            print(f"\nĐang tạo EPUB: {epub_path}")
            module.create_epub(url, title, author, chapters, epub_path, creator="Hishiro", language="vi")
            print(f"✔ EPUB đã tạo: {epub_path}")
        else:
            print("\nModule chưa có hàm create_epub(...).")
    except KeyboardInterrupt:
        print("\nĐã hủy.")
    except Exception as e:
        print(f"Lỗi: {e}")

if __name__ == "__main__":
    main()
