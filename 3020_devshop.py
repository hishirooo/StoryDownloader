# -*- coding: utf-8 -*-
import os, re, time, html, base64, hashlib, json
from typing import List, Dict
import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.2

def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""

def _fetch_html(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=_make_headers(url), timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "html.parser")

def _openssl_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int):
    d = b""; last = b""
    while len(d) < key_len + iv_len:
        last = hashlib.md5(last + password + salt).digest()
        d += last
    return d[:key_len], d[key_len:key_len+iv_len]

def _aes_encrypt_openssl(plaintext: str, passphrase: str) -> str:
    salt = get_random_bytes(8)
    key, iv = _openssl_bytes_to_key(passphrase.encode(), salt, 32, 16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pad = 16 - (len(plaintext.encode()) % 16)
    enc = cipher.encrypt(plaintext.encode() + bytes([pad])*pad)
    return base64.b64encode(b"Salted__" + salt + enc).decode()

def _aes_decrypt_openssl_raw(b64: str, passphrase: str) -> bytes:
    raw = base64.b64decode(b64)
    if not raw.startswith(b"Salted__"):
        raise ValueError("Không đúng format OpenSSL (Salted__).")
    salt = raw[8:16]
    key, iv = _openssl_bytes_to_key(passphrase.encode(), salt, 32, 16)
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(raw[16:])
    pad = dec[-1]
    if pad < 1 or pad > 16:
        raise ValueError("Padding không hợp lệ.")
    return dec[:-pad]

def _derive_key(obf: str) -> str:
    if "-" in obf:
        left, right = obf.split("-", 1)
    else:
        mid = (len(obf)+1)//2
        left, right = obf[:mid], obf[mid:] or "0000"
    prod = 1
    for ch in right:
        prod = (prod * ord(ch)) % 255
    out = []
    for i, ch in enumerate(left):
        code = ord(ch); r = (prod + i * code) % 26
        if 65 <= code <= 90:
            out.append(chr((code - 65 + r) % 26 + 65))
        elif 97 <= code <= 122:
            out.append(chr((code - 97 + r) % 26 + 97))
        else:
            out.append(ch)
    return "".join(out)

PASS = _derive_key("T5Hr41U5jKTTrtUOXdYZnyx3wjZEKUoxv16Clwwu4D5zIbd0-q9sdfh")

def _base_host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url)
    host = m.group(1).lower() if m else "3020.devshop.vn"
    # Hợp thức: 3020.devshop.vn hoặc metruyen.xyz
    if "metruyen" in host:
        return "metruyen.xyz"
    return "3020.devshop.vn"

def _api_base(url: str) -> str:
    host = _base_host(url)
    return f"https://{host}"

def _make_headers(url: str) -> Dict[str, str]:
    base = _api_base(url)
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": base,
        "Referer": url if "/story/" in url or "/chapter/" in url else base,
    }

def get_story_id(soup: BeautifulSoup) -> int:
    tag = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if tag and tag.string:
        data = json.loads(tag.string)
        return int(data["props"]["pageProps"]["story"]["id"])
    raise RuntimeError("Không tìm thấy story.id")

def get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    title = _text(soup.find("h1", class_="story_book-info__title__1jpSQ"))
    author = _text(soup.find("div", class_="story_book-info__author__lPhnG")).replace("Tác giả: ","")
    genres = " - ".join(_text(a) for a in soup.select(".story_book-info__category__B1RPT a"))
    cover_node = soup.select_one(".story_book__qq6xd img")
    cover = ""
    if cover_node and cover_node.get("src"):
        cover = _api_base(cover_node.get("src")) + cover_node["src"] if cover_node["src"].startswith("/") else cover_node["src"]
    desc = _text(soup.find("div", class_="story_card-content__NO3Br"))
    return {"title": title, "author": author, "genre": genres, "desc": desc, "cover": cover}

def get_list_chapters(story_url: str) -> List[Dict[str,str]]:
    soup = _fetch_html(story_url)
    sid = get_story_id(soup)
    api = _api_base(story_url) + "/api/chapters/list-chapters"

    chapters, page = [], 1
    while True:
        payload = {
            "id_story": _aes_encrypt_openssl(str(sid), PASS),
            "page": _aes_encrypt_openssl(str(page), PASS),
            "items_per_page": _aes_encrypt_openssl("50", PASS),
            "order": _aes_encrypt_openssl("asc", PASS),
        }
        r = requests.post(api, json=payload, headers=_make_headers(story_url), timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        raw = _aes_decrypt_openssl_raw(js["data"], PASS).decode("utf-8", "ignore")

        try:
            data = json.loads(raw)
        except Exception:
            data = []

        rows = data if isinstance(data, list) else (data.get("chapters") or data.get("data") or [])
        if not rows:
            break

        for ch in rows:
            slug = ch.get("slug") or ch.get("slug_chapter") or ""
            url = f"{_api_base(story_url)}/chapter/{slug}" if slug else ""
            chapters.append({"title": ch.get("name",""), "url": url})

        if len(rows) < 50:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
    return chapters

def _aes_cbc_decrypt_b64_ivprefix(b64_ct: str, key_bytes: bytes) -> str:
    """content_comp dạng base64, 16 byte đầu là IV"""
    raw = base64.b64decode(b64_ct)
    iv, enc = raw[:16], raw[16:]
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    dec = cipher.decrypt(enc)
    pad = dec[-1]
    if pad < 1 or pad > 16:
        raise ValueError("Padding không hợp lệ.")
    return dec[:-pad].decode("utf-8", "ignore")

def get_chapter_content_from_html(chapter_url: str) -> Dict[str, str]:
    soup = _fetch_html(chapter_url)
    node = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if not node or not node.string:
        raise RuntimeError("Không tìm thấy __NEXT_DATA__")

    data = json.loads(node.string)
    pp = data["props"]["pageProps"]
    chap = pp.get("chapter") or {}
    title = chap.get("title") or chap.get("name") or ""

    content_comp = chap.get("content_comp") or pp.get("contentEncrypt") or chap.get("content")
    key_encrypt  = chap.get("key_encrypt")

    if not content_comp:
        raise RuntimeError("Chưa có nội dung.")

    # Cách A: seed = decrypt(key_encrypt, PASS)
    tried = []
    def try_decrypt_with_seed(seed_bytes: bytes) -> str:
        key = hashlib.sha256(seed_bytes).digest()
        return _aes_cbc_decrypt_b64_ivprefix(content_comp, key)

    if key_encrypt:
        try:
            seed = _aes_decrypt_openssl_raw(key_encrypt, PASS)
            tried.append("A")
            return {"title": title, "content": try_decrypt_with_seed(seed)}
        except Exception:
            pass

    # Cách B: dùng ri -> rs[index]
    ri = pp.get("ri"); rs = pp.get("rs")
    if key_encrypt and isinstance(rs, dict) and isinstance(ri, str):
        try:
            ri_num = int(_aes_decrypt_openssl_raw(ri, PASS).decode("utf-8", "ignore").strip() or "0")
            # chọn 1 khóa phụ từ rs theo modulo
            keys = sorted(rs.keys(), key=lambda x: int(x) if x.isdigit() else x)
            if keys:
                pick = rs[keys[ri_num % len(keys)]]
                # đôi khi pick là 1 lớp OpenSSL nữa -> seed
                seed = _aes_decrypt_openssl_raw(pick, PASS)
                tried.append("B1")
                return {"title": title, "content": try_decrypt_with_seed(seed)}
        except Exception:
            # fallback B2: pick có thể đã là seed thô (base64)
            try:
                seed = base64.b64decode(pick)
                tried.append("B2")
                return {"title": title, "content": try_decrypt_with_seed(seed)}
            except Exception:
                pass

    # Cách C: key_encrypt là base64 seed
    if key_encrypt:
        try:
            seed = base64.b64decode(key_encrypt)
            tried.append("C")
            return {"title": title, "content": try_decrypt_with_seed(seed)}
        except Exception:
            pass

    raise RuntimeError("Chưa có nội dung.")

# ================= DEMO =================
if __name__ == "__main__":
    story = "https://3020.devshop.vn/story/bat-dau-tu-trang-do"
    if story.startswith("https://3020.devshop.vn/story/"):
        story = story.replace("https://3020.devshop.vn/story/", "https://metruyen.xyz/story/")
    soup = _fetch_html(story)
    info = get_book_info(soup)
    print("---- THÔNG TIN ----")
    for k,v in info.items(): print(f"{k}: {v}")

    print("\n---- DANH SÁCH CHƯƠNG ----")
    chaps = get_list_chapters(story)
    print(f"Tổng số chương: {len(chaps)}")
    for ch in chaps[:10]:
        print(f"- {ch['title']} -> {ch['url']}")

    if chaps:
        r = get_chapter_content_from_html(chaps[0]["url"])
        print("\nTiêu đề:", r["title"])
        print("\nNội dung rút gọn:\n", r["content"][:600], "...")
