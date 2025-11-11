# -*- coding: utf-8 -*-
"""
3020_devshop.py
- Tự động lấy danh sách + tải chương từ metruyen.xyz / 3020.devshop.vn
- Giải mã nội dung bằng:
    1) Python AES (mặc định)
    2) Fallback: chạy js.js (CryptoJS) qua execjs, có truyền PASS
- Nếu API thiếu content/key, fallback parse __NEXT_DATA__ trong HTML
- Ghi debug ra ./debug/
"""
import os, re, time, json, base64, hashlib, execjs
from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, List
import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

# ===== Config
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.15
DEBUG_DIR = Path("debug"); DEBUG_DIR.mkdir(exist_ok=True)
OUT_DIR   = Path("output"); OUT_DIR.mkdir(exist_ok=True)

def dump(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data, indent=2, ensure_ascii=False))

# ===== Helpers
def _text(el): return el.get_text(" ", strip=True) if el else ""
def _fetch_html(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def _base_from_url(u: str) -> str:
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"

# ===== OpenSSL AES helpers
def _openssl_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int):
    d, last = b"", b""
    while len(d) < key_len + iv_len:
        last = hashlib.md5(last + password + salt).digest()
        d += last
    return d[:key_len], d[key_len:key_len+iv_len]

def _aes_encrypt_openssl_str(plaintext: str, passphrase: str) -> str:
    salt = get_random_bytes(8)
    key, iv = _openssl_bytes_to_key(passphrase.encode(), salt, 32, 16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pad = 16 - len(plaintext.encode()) % 16
    ct  = cipher.encrypt(plaintext.encode() + bytes([pad]) * pad)
    return base64.b64encode(b"Salted__" + salt + ct).decode()

def _aes_decrypt_openssl_to_bytes(b64: str, passphrase: str) -> bytes:
    raw = base64.b64decode(b64)
    if not raw.startswith(b"Salted__"):
        raise ValueError("Không đúng định dạng OpenSSL 'Salted__'")
    salt = raw[8:16]
    key, iv = _openssl_bytes_to_key(passphrase.encode(), salt, 32, 16)
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(raw[16:])
    pad = dec[-1]
    if pad <= 0 or pad > 16:
        raise ValueError("Padding không hợp lệ (decrypt key_encrypt)")
    return dec[:-pad]

# ===== PASS derivation (giống các bản trước)
def _derive_key(obf: str) -> str:
    if "-" in obf: left, right = obf.split("-", 1)
    else:
        mid = (len(obf)+1)//2
        left, right = obf[:mid], obf[mid:] or "0000"
    prod = 1
    for ch in right:
        prod = (prod * ord(ch)) % 255
    out = []
    for i, ch in enumerate(left):
        code = ord(ch); r = (prod + i * code) % 26
        if 65 <= code <= 90:  out.append(chr((code - 65 + r) % 26 + 65))
        elif 97 <= code <= 122: out.append(chr((code - 97 + r) % 26 + 97))
        else: out.append(ch)
    return "".join(out)

PASS = _derive_key("T5Hr41U5jKTTrtUOXdYZnyx3wjZEKUoxv16Clwwu4D5zIbd0-q9sdfh")

# ===== Story info
def get_story_id(soup: BeautifulSoup) -> int:
    data = json.loads(soup.find("script", id="__NEXT_DATA__").string)
    return int(data["props"]["pageProps"]["story"]["id"])

def get_book_info(soup: BeautifulSoup, base: str) -> Dict[str, str]:
    title = _text(soup.find("h1", class_="story_book-info__title__1jpSQ"))
    author = _text(soup.find("div", class_="story_book-info__author__lPhnG")).replace("Tác giả: ","")
    genres = " - ".join(_text(a) for a in soup.select(".story_book-info__category__B1RPT a"))
    desc = _text(soup.find("div", class_="story_card-content__NO3Br"))
    img = soup.select_one(".story_book__qq6xd img")
    cover = base + img["src"] if img and img.has_attr("src") and not img["src"].startswith("http") else (img["src"] if img else "")
    return {"title": title, "author": author, "genre": genres, "desc": desc, "cover": cover}

# ===== List chapters
def get_list_chapters(story_url: str) -> List[Dict[str,str]]:
    base = _base_from_url(story_url)
    soup = _fetch_html(story_url)
    sid = get_story_id(soup)
    api = f"{base}/api/chapters/list-chapters"
    headers = {**HEADERS, "Origin": base, "Referer": story_url, "Content-Type": "application/json"}

    chapters, page = [], 1
    while True:
        payload = {
            "id_story": _aes_encrypt_openssl_str(str(sid), PASS),
            "page": _aes_encrypt_openssl_str(str(page), PASS),
            "items_per_page": _aes_encrypt_openssl_str("50", PASS),
            "order": _aes_encrypt_openssl_str("asc", PASS),
        }
        r = requests.post(api, json=payload, headers=headers, timeout=TIMEOUT)
        dump(DEBUG_DIR/f"list_page_{page}.json", r.text)
        js = r.json()
        enc = js.get("data")
        if not enc:
            break
        raw = _aes_decrypt_openssl_to_bytes(enc, PASS).decode("utf-8", "ignore")
        data = json.loads(raw)
        rows = data if isinstance(data, list) else data.get("chapters", []) or data.get("data", [])
        if not rows:
            break
        for c in rows:
            slug = c.get("slug") or c.get("slug_chapter") or ""
            chapters.append({"title": c.get("name",""), "url": f"{base}/chapter/{slug}"})
        if len(rows) < 50:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
    return chapters

# ===== JS fallback
def js_decrypt_with_pass(content_enc: str, key_enc: str, passphrase: str) -> str:
    js_path = Path("js.js")
    if not js_path.exists():
        raise RuntimeError("Thiếu js.js (CryptoJS) để fallback.")
    ctx = execjs.compile(js_path.read_text(encoding="utf-8"))
    # Không tồn tại is_callable trong execjs — dùng typeof
    typ = ctx.eval('typeof decryptContent')
    if typ != "function":
        raise RuntimeError("Không thấy decryptContent() trong js.js")
    return ctx.call("decryptContent", content_enc, key_enc, passphrase)

# ===== Python decrypt
def decrypt_chapter_payload_py(content_b64: str, key_b64: str) -> str:
    if not content_b64 or not key_b64:
        raise RuntimeError("Thiếu content_comp hoặc key_encrypt.")
    # 1) Giải key_encrypt (OpenSSL w/ PASS)
    seed = _aes_decrypt_openssl_to_bytes(key_b64, PASS)  # bytes
    # 2) SHA-256 làm key
    key = hashlib.sha256(seed).digest()
    # 3) content_comp = base64(iv + ciphertext)
    enc = base64.b64decode(content_b64)
    iv, ctb = enc[:16], enc[16:]
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(ctb)
    pad = dec[-1]
    if pad <= 0 or pad > 16:
        raise RuntimeError("Padding không hợp lệ (content_comp).")
    return dec[:-pad].decode("utf-8", "ignore")

# ===== get-chapter via API
def _get_chapter_via_api(chap_url: str) -> Dict[str, str]:
    base = _base_from_url(chap_url)
    slug = chap_url.split("/chapter/")[-1]
    api = f"{base}/api/chapters/get-chapter"
    headers = {**HEADERS, "Origin": base, "Referer": chap_url, "Content-Type": "application/json"}

    payload = {"slug": _aes_encrypt_openssl_str(slug, PASS)}
    r = requests.post(api, json=payload, headers=headers, timeout=TIMEOUT)
    dump(DEBUG_DIR/f"get_{slug}.json", r.text)
    js = r.json()
    enc = js.get("data")
    if not enc:
        return {}
    raw = _aes_decrypt_openssl_to_bytes(enc, PASS).decode("utf-8","ignore")
    data = json.loads(raw)
    if isinstance(data, list) and data:
        data = data[0]
    d = data.get("chapter") or data
    return {
        "title": d.get("title") or d.get("name"),
        "content_comp": d.get("content_comp") or d.get("contentEncrypt") or d.get("content"),
        "key_encrypt": d.get("key_encrypt"),
    }

# ===== Fallback HTML __NEXT_DATA__
def _get_chapter_via_html(chap_url: str) -> Dict[str, str]:
    soup = _fetch_html(chap_url)
    dump(DEBUG_DIR/"chapter.html", str(soup))
    node = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if not node or not node.string:
        return {}
    data = json.loads(node.string)
    dump(DEBUG_DIR/"chapter_nextdata.json", data)
    ch = data.get("props",{}).get("pageProps",{}).get("chapter",{})
    if not isinstance(ch, dict):
        return {}
    return {
        "title": ch.get("title") or ch.get("name"),
        "content_comp": ch.get("content_comp") or ch.get("contentEncrypt") or ch.get("content"),
        "key_encrypt": ch.get("key_encrypt"),
    }

def get_chapter(chap_url: str) -> Dict[str, str]:
    # 1) Thử API
    d = _get_chapter_via_api(chap_url)
    # 2) Nếu thiếu trường, thử HTML
    if not d.get("content_comp") or not d.get("key_encrypt"):
        d_html = _get_chapter_via_html(chap_url)
        d = {**d_html, **d}  # HTML ưu tiên điền chỗ trống

    title = d.get("title") or chap_url.rsplit("/",1)[-1]
    c = d.get("content_comp")
    k = d.get("key_encrypt")
    if not c or not k:
        raise RuntimeError("Chưa có nội dung (thiếu content_comp / key_encrypt).")

    # 3) Giải mã: Python → nếu fail thì JS
    try:
        html = decrypt_chapter_payload_py(c, k)
    except Exception as e_py:
        try:
            html = js_decrypt_with_pass(c, k, PASS)
        except Exception as e_js:
            dump(DEBUG_DIR/"decrypt_error.txt", f"PY: {e_py}\nJS: {e_js}")
            raise
    return {"title": title, "content": html}

# ===== Main
def main():
    story = input("URL truyện: ").strip()
    base = _base_from_url(story)
    soup = _fetch_html(story)
    info = get_book_info(soup, base)
    print(json.dumps(info, indent=2, ensure_ascii=False))

    chs = get_list_chapters(story)
    print(f"Tổng chương: {len(chs)}")

    # tải thử 1..3
    s, e = 1, min(3, len(chs))
    for i in range(s, e+1):
        ch = chs[i-1]
        print(f"Tải {i}/{len(chs)}: {ch['title']} ...")
        data = get_chapter(ch["url"])
        safe_title = re.sub(r'[\\/*?:"<>|]', '_', data["title"] or f"Chuong_{i:04d}")
        out = OUT_DIR/f"{safe_title}.txt"
        out.write_text(data["content"], encoding="utf-8")
        print("✓", out)

if __name__ == "__main__":
    main()
