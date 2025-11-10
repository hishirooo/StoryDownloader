# -*- coding: utf-8 -*-
"""
3020_devshop_fixed.py
- Lấy thông tin + danh sách chương từ 3020.devshop.vn
- Tự động nhận diện kiểu trả về (dict hoặc list)
"""

import os, re, time, html, unicodedata, base64, hashlib, json
from pathlib import Path
from typing import List, Dict
import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
import base64
import hashlib
import json
from Crypto.Cipher import AES
# =============== CẤU HÌNH ===============
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.2
API_LIST_CHAPS = "https://3020.devshop.vn/api/chapters/list-chapters"

# =============== TIỆN ÍCH ===============
def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""

def _fetch_html(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "html.parser")

# =============== AES / KEY ===============
def _openssl_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int):
    d = b""; last = b""
    while len(d) < key_len + iv_len:
        last = hashlib.md5(last + password + salt).digest()
        d += last
    return d[:key_len], d[key_len:key_len+iv_len]

def _aes_encrypt(plaintext: str, passphrase: str) -> str:
    salt = get_random_bytes(8)
    key, iv = _openssl_bytes_to_key(passphrase.encode(), salt, 32, 16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pad = 16 - (len(plaintext.encode()) % 16)
    enc = cipher.encrypt(plaintext.encode() + bytes([pad])*pad)
    return base64.b64encode(b"Salted__" + salt + enc).decode()

def _aes_decrypt(b64: str, passphrase: str) -> str:
    raw = base64.b64decode(b64)
    salt = raw[8:16]
    key, iv = _openssl_bytes_to_key(passphrase.encode(), salt, 32, 16)
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(raw[16:])
    pad = dec[-1]
    return dec[:-pad].decode()

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

# =============== TRUYỆN ===============
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
    desc = _text(soup.find("div", class_="story_card-content__NO3Br"))
    cover = "https://3020.devshop.vn" + soup.select_one(".story_book__qq6xd img")["src"]
    return {"title": title, "author": author, "genre": genres, "desc": desc, "cover": cover}

def get_list_chapters(story_url: str) -> List[Dict[str,str]]:
    soup = _fetch_html(story_url)
    sid = get_story_id(soup)
    chapters, page = [], 1
    while True:
        payload = {
            "id_story": _aes_encrypt(str(sid), PASS),
            "page": _aes_encrypt(str(page), PASS),
            "items_per_page": _aes_encrypt("50", PASS),
            "order": _aes_encrypt("asc", PASS),
        }
        r = requests.post(API_LIST_CHAPS, json=payload, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        raw = _aes_decrypt(r.json()["data"], PASS)
        data = json.loads(raw)

        # ✅ fix: API có thể trả list hoặc dict
        if isinstance(data, list):
            rows = data
        else:
            rows = data.get("chapters") or data.get("data") or []

        if not rows: break

        for ch in rows:
            slug = ch.get("slug") or ch.get("slug_chapter") or ""
            url = f"https://3020.devshop.vn/chapter/{slug}" if slug else ""
            chapters.append({"title": ch.get("name",""), "url": url})

        if len(rows) < 50: break
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
    return chapters


def _get_content_chapter(story_url: str) -> dict:
    soup = _fetch_html(story_url)

    # Ghi ra file HTML để debug
    with open("chapter.html", "w", encoding="utf-8") as f:
        f.write(str(soup))

    # Tìm thẻ script chứa dữ liệu JSON
    script_node = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if not script_node or not script_node.string:
        raise RuntimeError("Không tìm thấy __NEXT_DATA__")

    # Parse JSON
    data = json.loads(script_node.string)

    # Ghi ra file JSON để debug
    with open("chapter.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=4, ensure_ascii=False))

    # Trích xuất content_comp và key_encrypt
    try:
        content_comp = data["props"]["pageProps"]["chapter"]["content_comp"]
    except KeyError:
        content_comp = None  # hoặc raise nếu bạn muốn bắt lỗi

    try:
        key_encrypt = data["props"]["pageProps"]["chapter"]["key_encrypt"]
    except KeyError:
        key_encrypt = None
    print(f"content_comp: {content_comp}")
    print(f"key_encrypt: {key_encrypt}")
    return {
        "content_comp": content_comp,
        "key_encrypt": key_encrypt
    }
    

# =============== DEMO ===============
if __name__ == "__main__":
    story = "https://3020.devshop.vn/story/bat-dau-tu-trang-do"
    soup = _fetch_html(story)
    info = get_book_info(soup)
    print("---- THÔNG TIN ----")
    for k,v in info.items(): print(f"{k}: {v}")
    print("\n---- DANH SÁCH CHƯƠNG ----")
    chaps = get_list_chapters(story)
    print(f"Tổng số chương: {len(chaps)}")
    for ch in chaps[:10]:
        print(f"- {ch['title']} → {ch['url']}")
    _get_content_chapter(chaps[0]["url"])
