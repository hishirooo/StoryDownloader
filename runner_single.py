# -*- coding: utf-8 -*-
import sys, json
from pathlib import Path

try:
    import devshop_fixed as dev     # đã đổi tên file 3020_devshop_fixed.py -> devshop_fixed.py
except Exception:
    pass      # fallback nếu bạn vẫn giữ tên cũ

PREF_LIST_KEYS = ["chapters", "list", "items", "urls", "data", "result"]

def load_list_or_die(json_path: Path):
    data = json.loads(json_path.read_text(encoding="utf-8"))
    # Nếu đã là list -> OK
    if isinstance(data, list):
        return data
    # Nếu là dict -> thử các khóa thường gặp
    if isinstance(data, dict):
        for k in PREF_LIST_KEYS:
            v = data.get(k)
            if isinstance(v, list) and v:
                return v
        # Nếu dict ánh xạ id -> object/string, lấy values()
        vals = list(data.values())
        if vals and all(isinstance(x, (dict, str)) for x in vals):
            return vals
    # Nếu tới đây vẫn không ra list -> báo lỗi kèm hint
    print("File không phải là danh sách chương (list).")
    if isinstance(data, dict):
        print("Các khóa có trong file:", list(data.keys())[:20])
    raise SystemExit(2)

def extract_url(item):
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for k in ("url", "href", "link"):
            if k in item and item[k]:
                return item[k]
    return None

def main():
    if len(sys.argv) < 2:
        print("Usage: python runner_single.py <json_file>")
        raise SystemExit(1)

    json_path = Path(sys.argv[1])
    if not json_path.exists():
        print("Không tìm thấy file:", json_path)
        raise SystemExit(1)

    arr = load_list_or_die(json_path)
    # Lấy phần tử đầu tiên có URL hợp lệ
    url = None
    for it in arr:
        url = extract_url(it)
        if url:
            break
    if not url:
        print("Không tìm thấy trường 'url'/'href'/'link' trong danh sách.")
        raise SystemExit(3)

    print("Test với URL:", url)
    ch = dev.get_chapter(url)
    print("OK -> title:", ch.get("title"))
    print("Có content_comp?", bool(ch.get("content_comp")))
    print("Có key_encrypt?", bool(ch.get("key_encrypt")))

if __name__ == "__main__":
    main()
