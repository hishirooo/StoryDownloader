# -*- coding: utf-8 -*-
"""
3020_devshop_complete.py
- Lấy thông tin + danh sách chương + giải mã nội dung từ 3020.devshop.vn
"""

import os, re, time, html, unicodedata, base64, hashlib, json
from pathlib import Path
from typing import List, Dict
import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

# =============== CẤU HÌNH ===============
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 20
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
    d = b""
    last = b""
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
    """Giải mã AES CBC với OpenSSL format"""
    try:
        raw = base64.b64decode(b64)
        
        # Kiểm tra header "Salted__"
        if raw[:8] != b'Salted__':
            raise ValueError("Invalid format: missing 'Salted__' header")
        
        salt = raw[8:16]
        ciphertext = raw[16:]
        
        # Derive key và IV
        key, iv = _openssl_bytes_to_key(passphrase.encode('utf-8'), salt, 32, 16)
        
        # Giải mã
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(ciphertext)
        
        # Loại bỏ padding PKCS7
        pad = decrypted[-1]
        if isinstance(pad, str):
            pad = ord(pad)
        
        # Kiểm tra padding hợp lệ
        if pad > 16 or pad <= 0:
            raise ValueError(f"Invalid padding: {pad}")
        
        # Verify padding
        for i in range(pad):
            if decrypted[-(i+1)] != pad:
                raise ValueError("Invalid PKCS7 padding")
        
        return decrypted[:-pad].decode('utf-8')
        
    except Exception as e:
        print(f"❌ Lỗi giải mã: {e}")
        return None

def _derive_key(obf: str) -> str:
    """Derive key từ chuỗi obfuscated"""
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
        code = ord(ch)
        r = (prod + i * code) % 26
        if 65 <= code <= 90:
            out.append(chr((code - 65 + r) % 26 + 65))
        elif 97 <= code <= 122:
            out.append(chr((code - 97 + r) % 26 + 97))
        else:
            out.append(ch)
    return "".join(out)

# Master password
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

        # API có thể trả list hoặc dict
        if isinstance(data, list):
            rows = data
        else:
            rows = data.get("chapters") or data.get("data") or []

        if not rows: 
            break

        for ch in rows:
            slug = ch.get("slug") or ch.get("slug_chapter") or ""
            url = f"https://3020.devshop.vn/chapter/{slug}" if slug else ""
            chapters.append({"title": ch.get("name",""), "url": url})

        if len(rows) < 50: 
            break
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
    
    return chapters

# =============== NỘI DUNG CHƯƠNG ===============
def get_chapter_content(chapter_url: str) -> Dict[str, str]:
    """
    Lấy và giải mã nội dung chương
    
    Returns:
        dict: {
            "title": str,           # Tiêu đề chương (đã giải mã)
            "content": str,         # Nội dung chương (đã giải mã)
            "content_comp": str,    # Nội dung mã hóa (raw)
            "key_encrypt": str      # Key mã hóa (raw)
        }
    """
    soup = _fetch_html(chapter_url)
    
    # Tìm thẻ script chứa dữ liệu JSON
    script_node = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if not script_node or not script_node.string:
        raise RuntimeError("Không tìm thấy __NEXT_DATA__")

    # Parse JSON
    data = json.loads(script_node.string)
    chapter = data["props"]["pageProps"]["chapter"]
    
    # Lấy dữ liệu mã hóa
    content_comp = chapter.get("content_comp")
    key_encrypt = chapter.get("key_encrypt")
    title_encrypted = chapter.get("title")
    
    if not content_comp or not key_encrypt:
        raise RuntimeError("Không tìm thấy content_comp hoặc key_encrypt")
    
    print(f"📄 Chapter URL: {chapter_url}")
    print(f"   content_comp: {len(content_comp)} chars")
    print(f"   key_encrypt: {len(key_encrypt)} chars")
    
    # Bước 1: Giải mã key_encrypt để lấy password thực
    real_password = _aes_decrypt(key_encrypt, PASS)
    if not real_password:
        raise RuntimeError("Không thể giải mã key_encrypt")
    
    print(f"   ✓ Real password: {real_password}")
    
    # Bước 2: Giải mã title
    title = _aes_decrypt(title_encrypted, real_password) if title_encrypted else "Unknown"
    
    # Bước 3: Giải mã content_comp
    content = _aes_decrypt(content_comp, real_password)
    if not content:
        raise RuntimeError("Không thể giải mã content_comp")
    
    print(f"   ✓ Content: {len(content)} chars")
    
    return {
        "title": title,
        "content": content,
        "content_comp": content_comp,
        "key_encrypt": key_encrypt
    }

# =============== DEMO ===============
if __name__ == "__main__":
    print("="*70)
    print("🔐 GIẢI MÃ NỘI DUNG TỪ 3020.DEVSHOP.VN")
    print("="*70)
    
    story = "https://3020.devshop.vn/story/bat-dau-tu-trang-do"
    
    # 1. Lấy thông tin truyện
    print("\n📚 Đang lấy thông tin truyện...")
    soup = _fetch_html(story)
    info = get_book_info(soup)
    
    print("\n---- THÔNG TIN TRUYỆN ----")
    for k, v in info.items(): 
        print(f"{k}: {v}")
    
    # 2. Lấy danh sách chương
    print("\n📖 Đang lấy danh sách chương...")
    chaps = get_list_chapters(story)
    print(f"✓ Tổng số chương: {len(chaps)}")
    
    print("\n---- 10 CHƯƠNG ĐẦU ----")
    for ch in chaps[:10]:
        print(f"- {ch['title']}")
    
    # 3. Giải mã nội dung chương đầu tiên
    if chaps:
        print("\n" + "="*70)
        print("📄 GIẢI MÃ CHƯƠNG ĐẦU TIÊN")
        print("="*70)
        
        try:
            chapter_data = get_chapter_content(chaps[0]["url"])
            
            print(f"\n✓ Tiêu đề: {chapter_data['title']}")
            print(f"✓ Độ dài: {len(chapter_data['content'])} ký tự")
            
            print("\n---- NỘI DUNG (500 KÝ TỰ ĐẦU) ----")
            print(chapter_data['content'][:500])
            print("...")
            
            # Lưu ra file
            output_file = "chapter_content.txt"
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(f"Tiêu đề: {chapter_data['title']}\n")
                f.write("="*70 + "\n\n")
                f.write(chapter_data['content'])
            
            print(f"\n💾 Đã lưu nội dung vào: {output_file}")
            
        except Exception as e:
            print(f"❌ Lỗi: {e}")
            import traceback
            traceback.print_exc()